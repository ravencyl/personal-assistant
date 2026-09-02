from django.db import models
from django.conf import settings

# 一轮对话在 Qoder 侧从发问到回完的上限（秒）。超过就把 turn 定为 error。
# 这个值不再受 gunicorn --timeout 约束（请求里不 sleep 了），只需大于用户
# 能接受的等待：联网问答多轮检索确实可能跑到一分多钟。
TURN_TTL_SECONDS = 180

# 「session 已 idle 但本轮还提不到文本」的宽限期（秒）。
# 平台的 status 切换比事件写入快，不区分「还没同步完」和「真的没回复」就会把
# 正常的回复误判成空回复（旧同步轮询里那个 idle_hits >= 3 就是在防这个，
# 现在轮询跨请求了，拿时间代替计数器）。
TURN_IDLE_GRACE_SECONDS = 12


class Conversation(models.Model):
    """AI 对话"""
    # ── 异步 turn 状态机 ──
    # 一个 session 同时只能处理一轮（发第二条会拿 409），旧实现因此在请求里
    # sleep 轮询到 AI 回完：一个提问占住一个 gunicorn worker 最长 90s（一共只有
    # 3 个），而且用户不能取消、不能接着打字、刷新就丢这一轮。
    # 现在发送立即返回，等 AI 的循环搬到轮询端点上，状态全部落库 —— 所以刷新能续上。
    TURN_NONE = 'none'            # 没有进行中的轮次（历史对话 / 刚建完）
    TURN_QUEUED = 'queued'        # 用户消息已存，尚未确认发到 Qoder（撞 409 也回到这里重试）
    TURN_AWAITING = 'awaiting'    # 已发到 Qoder，等 assistant 文本
    TURN_FINALIZING = 'finalizing'  # 某个轮询已认领收尾：跑编排器 + 落库（多标签页只会有一个抢到）
    TURN_DONE = 'done'            # 本轮已落库
    TURN_ERROR = 'error'          # 超时 / 用户取消
    TURN_CHOICES = [
        (TURN_NONE, '无'),
        (TURN_QUEUED, '待发送'),
        (TURN_AWAITING, '等待回复'),
        (TURN_FINALIZING, '收尾中'),
        (TURN_DONE, '已完成'),
        (TURN_ERROR, '已中断'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversations'
    )
    session_id = models.CharField(max_length=64, unique=True, help_text='Qoder sess_xxx ID')
    agent_id = models.CharField(max_length=64, help_text='Qoder agent_xxx ID')
    title = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ('idle', '空闲'),
            ('processing', '处理中'),
            ('archived', '已归档'),
        ],
        default='idle'
    )
    turn_state = models.CharField(
        max_length=20, choices=TURN_CHOICES, default=TURN_NONE,
        help_text='当前轮次状态（异步收发状态机，刷新页面能续上全靠它）',
    )
    turn_started_at = models.DateTimeField(null=True, blank=True,
                                           help_text='本轮开始时间，TTL 超时判定用')
    turn_idle_at = models.DateTimeField(null=True, blank=True,
                                        help_text='首次观察到“已 idle 但无本轮文本”的时刻，宽限期判定用')
    turn_message = models.ForeignKey(
        'chat.Message', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
        help_text='本轮待回答的那条用户消息（不靠「取最后一条 user」推导：'
                  '异步后用户可以在等回复时接着打字，推导会配错对象）',
    )
    turn_prompt = models.TextField(
        blank=True,
        help_text='实际发给 Qoder 的完整文本（页面上下文 + 知识库注入 + 用户原文）。'
                  '必须存：把它塞进用户消息会污染历史（现有约定只存原文），'
                  '不存则无法重试发送（首帧协议还在跑时发第二条会撞 409）',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = '对话'
        verbose_name_plural = '对话'

    def __str__(self):
        return self.title or f'对话 {self.session_id[:12]}...'

    @property
    def last_message(self):
        # 优先使用列表页预取的 last_message_list，避免 N+1 查询
        prefetched = getattr(self, 'last_message_list', None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.messages.order_by('-created_at').first()

    @property
    def turn_active(self):
        """是否有进行中的一轮（前端据此展示进度位与恢复轮询）"""
        return self.turn_state in (self.TURN_QUEUED, self.TURN_AWAITING, self.TURN_FINALIZING)

    def turn_expired(self, now=None):
        """本轮是否已超时（TTL 到了但状态还停在中间态：worker 被杀、浏览器关掉等）"""
        if not self.turn_active or not self.turn_started_at:
            return False
        from django.utils import timezone
        now = now or timezone.now()
        return (now - self.turn_started_at).total_seconds() > TURN_TTL_SECONDS

    def claim_turn(self, from_state, to_state):
        """原子地抢一个轮次状态转换，返回 True 表示本请求抢到收尾权

        必须是条件 UPDATE：两条轮询请求（两个标签页，或用户狂点重试）都看到
        awaiting 时，如果各自跑一遍编排器，就会落库两条一模一样的 assistant
        消息（并且工具被执行两次）。SQLite 下单行 UPDATE 自带原子性，不需要额外锁。
        """
        updated = Conversation.objects.filter(
            id=self.id, turn_state=from_state,
        ).update(turn_state=to_state)
        if updated:
            self.turn_state = to_state
        return bool(updated)

    def reset_turn(self):
        """回到「无进行中的轮次」（发送前的失败路径 / 归档时用）"""
        self.turn_state = self.TURN_NONE
        self.turn_started_at = None
        self.turn_idle_at = None
        self.turn_message = None
        self.turn_prompt = ''
        self.save(update_fields=['turn_state', 'turn_started_at', 'turn_prompt',
                                 'turn_idle_at', 'turn_message', 'updated_at'])


class Message(models.Model):
    """对话消息"""
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    role = models.CharField(
        max_length=20,
        choices=[
            ('user', '用户'),
            ('assistant', '助手'),
            ('system', '系统'),
        ]
    )
    content = models.TextField()
    event_type = models.CharField(max_length=50, blank=True)
    payload = models.JSONField(
        null=True, blank=True,
        help_text='结构化卡片协议：{"card": "activity|activity_list|...", "activity_ids": [...], "action": {...}}',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = '消息'
        verbose_name_plural = '消息'

    def __str__(self):
        return f'[{self.role}] {self.content[:50]}'
