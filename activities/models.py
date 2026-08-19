from django.db import models
from django.db.models import Sum
from django.conf import settings


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
    start_date = models.DateField('开始日期')
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
    cost = models.DecimalField(
        '费用金额',
        max_digits=10,
        decimal_places=2,
        default=0
    )
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

    def __str__(self):
        return self.name

    @property
    def children_cost(self):
        """直接子活动的费用合计"""
        total = self.children.aggregate(total=Sum('cost'))['total']
        return total or 0

    @property
    def total_cost(self):
        """累计费用：自身费用 + 所有后代活动费用"""
        total = self.cost or 0
        for child in self.children.all():
            total += child.total_cost
        return total

    @property
    def date_range(self):
        """日期范围展示"""
        if self.end_date:
            return f'{self.start_date} ~ {self.end_date}'
        return str(self.start_date)
