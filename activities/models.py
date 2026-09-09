from django.db import models
from django.db.models import Sum
from django.conf import settings
from taggit.managers import TaggableManager

from .categories import category_label_map


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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


class ExpenseCategory(models.Model):
    """费用类别注册表：类别的增/删/改/停用全在 admin 里完成，零代码变更

    取舍：Expense.category 保留字符串 key（不做 FK）——
    - 优点：存量数据零迁移风险（key 原样保留）；删除/停用类别不影响历史
      记录的展示与统计（label 查不到时回落 key 本身）；无级联/SET_NULL 的
      语义复杂性；费用聚合不用 join。
    - 代价：数据库层无引用完整性。用「写入只走 services.clean_category 单
      一入口 + 展示回落」兑住，脏 key 不会静默产生，出现了也不报错。
    - 若改 FK：迁移需先建表回填、停用/删除要处理 SET_NULL 丢信息问题、
      报表聚合多一次 join，收益（约束）不抵成本，已否决。
    """
    key = models.CharField('标识', max_length=20, unique=True)
    label = models.CharField('显示名', max_length=20)
    sort = models.PositiveIntegerField('排序号', default=100)
    is_active = models.BooleanField('启用', default=True)

    class Meta:
        ordering = ['sort', 'id']
        verbose_name = '费用类别'
        verbose_name_plural = '费用类别'

    def __str__(self):
        return f'{self.label}（{self.key}）'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .categories import invalidate_category_cache
        invalidate_category_cache()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        from .categories import invalidate_category_cache
        invalidate_category_cache()
        return result


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
    # 类别存 ExpenseCategory.key（字符串），不设 choices、不做 FK——取舍理由
    # 见 ExpenseCategory docstring。可选值/显示名由 activities.categories
    # 从类别表动态提供（表单下拉/清洗校验/展示统计三处口径见该模块 docstring）。
    category = models.CharField('类别', max_length=20, default='other')
    paid_at = models.DateField('消费日期', null=True, blank=True)
    note = models.CharField('备注', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def get_category_display(self):
        """类别中文名（覆盖 Django 自动生成的同名方法：字段已无 choices）。

        全量口径（含停用类别）——停用类别的历史费用照常显示；
        类别被删（key 查不到）时回落 key 本身，绝不报错。
        """
        return category_label_map().get(self.category, self.category)

    class Meta:
        ordering = ['-paid_at', '-created_at']
        verbose_name = '费用'
        verbose_name_plural = '费用'
        indexes = [
            models.Index(fields=['user', 'category', '-paid_at']),
        ]

    def __str__(self):
        label = self.get_category_display()
        return f'¥{self.amount} [{label}] {self.note or ""}'.strip()


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
