from django.db import models
from django.conf import settings


class Reminder(models.Model):
    """定时提醒"""
    STATUS_CHOICES = [
        ('pending', '待触发'),
        ('fired', '已触发'),
        ('dismissed', '已忽略'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reminders',
    )
    content = models.CharField('提醒内容', max_length=255)
    trigger_at = models.DateTimeField('触发时间', db_index=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    related_activity = models.ForeignKey(
        'activities.Activity',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='reminders',
        verbose_name='关联活动',
    )
    source_message = models.ForeignKey(
        'chat.Message',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='reminders',
        verbose_name='来源对话消息',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['trigger_at']
        verbose_name = '提醒'
        verbose_name_plural = '提醒'
        indexes = [
            models.Index(fields=['status', 'trigger_at']),
            models.Index(fields=['user', 'status']),
        ]

    def __str__(self):
        return f'{self.content} ({self.get_status_display()})'


class DailySummary(models.Model):
    """每日晚间摘要：cron 预生成，Daily 页展示"""
    STATUS_CHOICES = [
        ('pending', '待生成'),
        ('ready', '已生成'),
        ('fallback', '降级生成'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='daily_summaries',
    )
    summary_date = models.DateField('摘要日期')
    content = models.TextField('摘要内容', blank=True, default='')
    stats = models.JSONField('统计数据', default=dict, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    generated_at = models.DateTimeField('生成时间', null=True, blank=True)

    class Meta:
        verbose_name = '每日摘要'
        verbose_name_plural = '每日摘要'
        unique_together = [('user', 'summary_date')]
        indexes = [
            models.Index(fields=['user', 'summary_date']),
        ]

    def __str__(self):
        return f'{self.user} {self.summary_date} ({self.get_status_display()})'


class SuggestionState(models.Model):
    """建议关闭/已读状态，按指纹幂等记录"""
    ACTION_CHOICES = [
        ('dismissed', '已关闭'),
        ('read', '已读'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='suggestion_states',
    )
    fingerprint = models.CharField('建议指纹', max_length=128, db_index=True)
    action = models.CharField('操作', max_length=20, choices=ACTION_CHOICES, default='dismissed')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '建议状态'
        verbose_name_plural = '建议状态'
        unique_together = [('user', 'fingerprint')]
        indexes = [
            models.Index(fields=['user', 'action']),
        ]

    def __str__(self):
        return f'{self.user} {self.fingerprint} ({self.get_action_display()})'


class DailyInsight(models.Model):
    """AI 个性化洞察：cron 每日预生成，Daily 页展示"""
    STATUS_CHOICES = [
        ('pending', '待生成'),
        ('ready', '已生成'),
        ('fallback', '降级生成'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='daily_insights',
    )
    insight_date = models.DateField('洞察日期')
    insights = models.JSONField('洞察列表', default=list, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    generated_at = models.DateTimeField('生成时间', null=True, blank=True)

    class Meta:
        verbose_name = '每日洞察'
        verbose_name_plural = '每日洞察'
        unique_together = [('user', 'insight_date')]
        indexes = [
            models.Index(fields=['user', 'insight_date']),
        ]

    def __str__(self):
        return f'{self.user} {self.insight_date} ({self.get_status_display()})'


def check_due_reminders(user):
    """检查并触发到期的提醒，返回触发的提醒列表"""
    from django.utils import timezone
    now = timezone.now()
    due = Reminder.objects.filter(
        user=user,
        status='pending',
        trigger_at__lte=now,
    )
    count = due.update(status='fired')
    if count:
        return list(
            Reminder.objects.filter(user=user, status='fired', trigger_at__lte=now)
            .order_by('-trigger_at')[:count]
        )
    return []
