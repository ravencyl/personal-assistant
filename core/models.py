from django.db import models
from django.conf import settings


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
