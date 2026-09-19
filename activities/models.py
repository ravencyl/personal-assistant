from django.db import models
from django.db.models import Sum
from django.conf import settings
from django.utils import timezone
import secrets

from core.models import Tag


class Participant(models.Model):
    """活动参与者"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='participants'
    )
    name = models.CharField('姓名', max_length=100)
    note = models.CharField('备注', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = '参与者'
        verbose_name_plural = '参与者'

    def __str__(self):
        return self.name


class Activity(models.Model):
    """活动记录"""
    STATUS_CHOICES = [
        ('planned', '计划'),
        ('in_progress', '进行中'),
        ('done', '已完成'),
        ('cancelled', '已取消'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='activities'
    )
    name = models.CharField('活动名称', max_length=255)
    description = models.TextField('活动描述', blank=True)
    start_date = models.DateField('开始日期', null=True, blank=True)
    end_date = models.DateField('结束日期', null=True, blank=True)
    # 具体时间（可选，2026-09-17）：只填日期不填时间的活动行为与展示完全不变；
    # 带时间的活动导出日历时用 DATE-TIME（含时区）替代全天事件，Apple 日历准点提醒
    start_time = models.TimeField('开始时间', null=True, blank=True)
    end_time = models.TimeField('结束时间', null=True, blank=True)
    status = models.CharField(
        '状态',
        max_length=20,
        choices=STATUS_CHOICES,
        default='planned'
    )
    participants = models.ManyToManyField(
        Participant,
        blank=True,
        related_name='activities',
        verbose_name='参与者'
    )
    # 反向名用默认（tag.activity_set / query name 'activity'），与 core.tags 的
    # _SCOPE_QUERY_NAMES 映射对齐；不要设 related_name（会连带改 query name）
    tags = models.ManyToManyField(Tag, blank=True, verbose_name='标签')
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='children',
        verbose_name='父活动'
    )
    # 活动依赖关系：前置未完成时后续活动标灰提醒
    # symmetrical=False：A 阻塞 B 不等于 B 阻塞 A
    blocked_by = models.ManyToManyField(
        'self',
        symmetrical=False,
        blank=True,
        related_name='blocking',
        verbose_name='前置依赖',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField('归档时间', null=True, blank=True)

    class Meta:
        ordering = ['-start_date', '-created_at']
        verbose_name = '活动'
        verbose_name_plural = '活动'
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', '-start_date']),
        ]

    def __str__(self):
        return self.name

    @property
    def total_cost(self):
        """该活动的费用合计（直接关联的 Expense 总额）"""
        return self.expenses.aggregate(s=Sum('amount'))['s'] or 0

    @property
    def date_range(self):
        """日期范围展示（带具体时间时附在对应日期后）

        时间用 str()[:5] 而非 {t:%H:%M}：实例内存里 time 可能仍是未经字段
        转换的 str（同 PushSchedule.__str__ 踩过的坑），format spec 会炸。
        """

        def fmt(d, t):
            return str(d) + (f' {str(t)[:5]}' if t else '')

        st = str(self.start_time)[:5] if self.start_time else ''
        et = str(self.end_time)[:5] if self.end_time else ''
        if self.start_date and self.end_date:
            if self.start_date == self.end_date:
                if st and et and et > st:
                    return f'{self.start_date} {st} ~ {et}'
                return fmt(self.start_date, self.start_time)
            return f'{fmt(self.start_date, self.start_time)} ~ {fmt(self.end_date, self.end_time)}'
        if self.start_date:
            return fmt(self.start_date, self.start_time)
        if self.end_date:
            return f'~ {fmt(self.end_date, self.end_time)}'
        return '未设定'

    def is_blocked(self):
        """检查此活动是否被阻塞（前置依赖中有未完成的活动）"""
        if not self.pk:
            return False
        return self.blocked_by.exclude(status='done').exists()

    def blocking_names(self):
        """返回阻塞此活动的未完成前置依赖名称列表"""
        if not self.pk:
            return []
        return list(
            self.blocked_by.exclude(status='done')
            .values_list('name', flat=True)
        )

    def would_create_cycle(self, target_ids):
        """检测将 blocked_by 设为 target_ids 是否会形成环

        环检测策略：从每个 target 出发沿 blocked_by 反向链（blocking）向上走，
        如果能走到 self，说明形成了环。
        """
        if not self.pk or not target_ids:
            return False
        target_ids = set(target_ids)
        if self.pk in target_ids:
            return True  # 自己依赖自己

        # BFS：从每个 target 出发，沿 blocking 反向链查找是否可达 self
        visited = set()
        queue = list(target_ids)
        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)
            if current_id == self.pk:
                return True  # 从 target 能走到 self，形成环
            # 找 current 的 blocked_by（它的前置依赖）
            try:
                blockers = list(
                    Activity.objects.filter(id=current_id)
                    .values_list('blocked_by__id', flat=True)
                )
                queue.extend(b for b in blockers if b and b not in visited)
            except Exception:
                pass
        return False


class ActivityLog(models.Model):
    """活动操作日志（创建/编辑/删除/子任务/状态变更）"""
    ACTION_CHOICES = [
        ('created', '创建了活动'),
        ('edited', '编辑了活动'),
        ('deleted', '删除了活动'),
        ('sub_created', '创建了子任务'),
        ('status_changed', '修改了状态'),
        ('commented', '评论了活动'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='activity_logs',
        verbose_name='操作人'
    )
    activity = models.ForeignKey(
        Activity,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='logs',
        verbose_name='关联活动'
    )
    activity_name = models.CharField('活动名称', max_length=255)
    action = models.CharField('操作类型', max_length=20, choices=ACTION_CHOICES)
    summary = models.TextField('变更摘要', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '活动日志'
        verbose_name_plural = '活动日志'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.user.username} {self.get_action_display()} {self.activity_name}'


class ActivityComment(models.Model):
    """活动评论（追加式讨论时间线，正序展示）

    可见性跟随活动：能看见活动的人（所有者/超级用户）就能看见其评论，
    不单独设 user 过滤——视图层经 get_visible(Activity) 门禁后才能触达。
    AI 决策链路：activities.get / activities.comments 工具读取，
    activities.add_comment 工具写入（写操作同样走 log_activity）。
    """
    activity = models.ForeignKey(
        Activity,
        on_delete=models.CASCADE,
        related_name='comments',
        verbose_name='活动'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='activity_comments',
        verbose_name='评论人'
    )
    content = models.TextField('评论内容')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = '活动评论'
        verbose_name_plural = '活动评论'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.user.username} @ {self.activity.name}: {self.content[:20]}'


class Expense(models.Model):
    """费用条目（一个活动可关联 0~N 条费用）"""

    activity = models.ForeignKey(
        Activity,
        on_delete=models.CASCADE,
        related_name='expenses',
        verbose_name='关联活动'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='expenses',
        verbose_name='归属用户'
    )
    amount = models.DecimalField('金额', max_digits=10, decimal_places=2)
    # 费用标签（2026-09 标签体系整合）：与活动标签同属 core.Tag，
    # scope='expense' 隔离；原字符串 category 字段已并入标签体系（0017）
    tags = models.ManyToManyField(Tag, blank=True, verbose_name='标签')
    paid_at = models.DateField('消费日期', null=True, blank=True)
    note = models.CharField('备注', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-paid_at', '-created_at']
        verbose_name = '费用'
        verbose_name_plural = '费用'
        indexes = [
            models.Index(fields=['user', '-paid_at']),
        ]

    def __str__(self):
        return f'¥{self.amount} {self.note or ""}'.strip()


class Attachment(models.Model):
    """活动附件（文件/图片）"""
    activity = models.ForeignKey(
        Activity,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='关联活动'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='上传用户'
    )
    file = models.FileField('文件', upload_to='attachments/%Y/%m/')
    filename = models.CharField('原始文件名', max_length=255)
    content_type = models.CharField('MIME 类型', max_length=100, blank=True)
    size = models.PositiveIntegerField('文件大小（字节）', default=0)
    uploaded_at = models.DateTimeField('上传时间', auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']
        verbose_name = '附件'
        verbose_name_plural = '附件'

    def __str__(self):
        return self.filename

    @property
    def is_image(self):
        return self.content_type.startswith('image/') if self.content_type else False

    @property
    def size_display(self):
        if self.size < 1024:
            return f'{self.size} B'
        elif self.size < 1024 * 1024:
            return f'{self.size / 1024:.1f} KB'
        else:
            return f'{self.size / (1024 * 1024):.1f} MB'


class CalendarFeed(models.Model):
    """日历订阅令牌：ICS/webcal 订阅源的鉴权凭据（2026-09-11）。

    订阅端点无法携带会话（Apple 日历服务器代为拉取），故用
    「长随机、不可猜测、与用户绑定」的 token 鉴权；可在设置页
    重新生成（旧 token 立即失效）或吊销。
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='calendar_feed',
        verbose_name='归属用户'
    )
    token = models.CharField('订阅令牌', max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    revoked_at = models.DateTimeField('吊销时间', null=True, blank=True)

    class Meta:
        verbose_name = '日历订阅令牌'
        verbose_name_plural = '日历订阅令牌'

    def __str__(self):
        return f'{self.user.username} 的日历订阅'

    @classmethod
    def issue(cls, user):
        """取现有令牌；不存在则签发（幂等）。"""
        feed, _ = cls.objects.get_or_create(user=user)
        if not feed.token:
            feed.token = secrets.token_urlsafe(32)
            feed.save(update_fields=['token'])
        return feed

    def regenerate(self):
        """换发新 token，旧 URL 立即失效；同时解除吊销。"""
        self.token = secrets.token_urlsafe(32)
        self.revoked_at = None
        self.save(update_fields=['token', 'revoked_at'])

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=['revoked_at'])

    @property
    def active(self):
        return self.revoked_at is None


class ActivityTemplate(models.Model):
    """活动模板：保存常用活动的字段快照，创建时一键填充"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='activity_templates',
    )
    name = models.CharField('模板名称', max_length=100)
    default_tags = models.CharField('默认标签', max_length=255, blank=True,
                                    help_text='逗号分隔')
    default_participants = models.CharField('默认参与者', max_length=255, blank=True,
                                            help_text='逗号分隔')
    default_budget = models.DecimalField('默认预算', max_digits=10, decimal_places=2,
                                          null=True, blank=True)
    default_description = models.TextField('默认描述', blank=True)
    use_count = models.IntegerField('使用次数', default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-use_count', '-created_at']
        verbose_name = '活动模板'
        verbose_name_plural = '活动模板'

    def __str__(self):
        return self.name


class DailySuggestion(models.Model):
    """每日 AI 建议缓存：cron 每日 00:00 预计算，对话里的 daily 简报卡直接读取，支持手动刷新"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='daily_suggestions',
    )
    date = models.DateField('日期')
    suggestion = models.TextField('建议内容', blank=True)
    is_ai = models.BooleanField('是否 AI 生成', default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'date')
        ordering = ['-date']
        verbose_name = '每日建议'
        verbose_name_plural = '每日建议'

    def __str__(self):
        return f'{self.user.username} {self.date}'
