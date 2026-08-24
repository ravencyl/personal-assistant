from django.db import models
from django.conf import settings


class Conversation(models.Model):
    """AI 对话"""
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
