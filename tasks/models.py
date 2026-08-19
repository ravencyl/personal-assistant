from django.db import models
from django.conf import settings


class Task(models.Model):
    """任务"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='tasks'
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    priority = models.IntegerField(
        default=0,
        choices=[
            (0, '无'),
            (1, '低'),
            (2, '中'),
            (3, '高'),
        ]
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ('pending', '待办'),
            ('in_progress', '进行中'),
            ('done', '已完成'),
            ('cancelled', '已取消'),
        ],
        default='pending'
    )
    due_date = models.DateTimeField(null=True, blank=True)
    reminder_time = models.DateTimeField(null=True, blank=True)
    reminder_sent = models.BooleanField(default=False)
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='subtasks'
    )
    ai_generated = models.BooleanField(default=False, help_text='是否由 AI 生成')
    project = models.CharField(max_length=100, blank=True)
    activity = models.ForeignKey(
        'activities.Activity',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tasks',
        verbose_name='所属活动'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-priority', 'due_date', '-created_at']
        verbose_name = '任务'
        verbose_name_plural = '任务'

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        from django.utils import timezone
        if self.due_date and self.status not in ('done', 'cancelled'):
            return timezone.now() > self.due_date
        return False
