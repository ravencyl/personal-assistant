from django.db import models
from django.db.models import Sum
from django.conf import settings
from taggit.managers import TaggableManager


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
    tags = TaggableManager(blank=True, verbose_name='标签')
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='children',
        verbose_name='父活动'
    )
    source_message = models.ForeignKey(
        'chat.Message',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_activities',
        verbose_name='来源对话消息'
    )
    recurring_source = models.ForeignKey(
        'RecurringActivity',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='generated_activities',
        verbose_name='循环来源'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_date', '-created_at']
        verbose_name = '活动'
        verbose_name_plural = '活动'

    def __str__(self):
        return self.name

    @property
    def total_cost(self):
        """该活动的费用合计（直接关联的 Expense 总额）"""
        return self.expenses.aggregate(s=Sum('amount'))['s'] or 0

    @property
    def date_range(self):
        """日期范围展示"""
        if self.start_date and self.end_date:
            return f'{self.start_date} ~ {self.end_date}'
        if self.start_date:
            return str(self.start_date)
        if self.end_date:
            return f'~ {self.end_date}'
        return '未设定'


class ActivityLog(models.Model):
    """活动操作日志（创建/编辑/删除/子任务/状态变更）"""
    ACTION_CHOICES = [
        ('created', '创建了活动'),
        ('edited', '编辑了活动'),
        ('deleted', '删除了活动'),
        ('sub_created', '创建了子任务'),
        ('status_changed', '修改了状态'),
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


class ActivityTemplate(models.Model):
    """活动模板（预设子任务结构，一键创建）"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='activity_templates'
    )
    name = models.CharField('模板名称', max_length=255)
    description = models.TextField('模板描述', blank=True)
    default_children = models.JSONField(
        '预设子任务', default=list, blank=True,
        help_text='[{"name": "订票"}, {"name": "住宿"}]'
    )
    default_tags = models.JSONField('预设标签', default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '活动模板'
        verbose_name_plural = '活动模板'

    def __str__(self):
        return self.name


class Expense(models.Model):
    """费用条目（一个活动可关联 0~N 条费用）"""
    CATEGORY_CHOICES = [
        ('transport', '交通'), ('accommodation', '住宿'), ('food', '餐饮'),
        ('ticket', '门票'), ('shopping', '购物'), ('work', '工作'),
        ('other', '其他'),
    ]

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
    category = models.CharField(
        '类别', max_length=20, choices=CATEGORY_CHOICES, default='other'
    )
    paid_at = models.DateField('消费日期', null=True, blank=True)
    note = models.CharField('备注', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-paid_at', '-created_at']
        verbose_name = '费用'
        verbose_name_plural = '费用'

    def __str__(self):
        label = self.get_category_display()
        return f'¥{self.amount} [{label}] {self.note or ""}'.strip()


class RecurringActivity(models.Model):
    """循环活动/习惯追踪"""
    FREQUENCY_CHOICES = [
        ('daily', '每日'),
        ('weekly', '每周'),
        ('monthly', '每月'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='recurring_activities'
    )
    name = models.CharField('习惯名称', max_length=255)
    frequency = models.CharField('频率', max_length=10, choices=FREQUENCY_CHOICES, default='daily')
    day_of_week = models.IntegerField(
        '星期几', null=True, blank=True,
        help_text='0=周一...6=周日（仅每周时有效）'
    )
    day_of_month = models.IntegerField(
        '每月几号', null=True, blank=True,
        help_text='1-28（仅每月时有效）'
    )
    status = models.CharField(
        '状态', max_length=20,
        choices=Activity.STATUS_CHOICES,
        default='planned'
    )
    is_active = models.BooleanField('启用', default=True)
    last_generated_date = models.DateField('上次生成日期', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', '-created_at']
        verbose_name = '循环活动'
        verbose_name_plural = '循环活动'

    def __str__(self):
        freq_label = dict(self.FREQUENCY_CHOICES).get(self.frequency, self.frequency)
        return f'{self.name}（{freq_label}）'


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
