from django.db import models
from django.conf import settings


class Memory(models.Model):
    """AI 长期记忆——跨对话持久化的用户信息"""
    CATEGORY_CHOICES = [
        ('preference', '偏好'),
        ('fact', '事实'),
        ('goal', '目标'),
        ('relationship', '关系'),
        ('habit', '习惯'),
        ('other', '其他'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memories',
    )
    content = models.CharField('记忆内容', max_length=500)
    category = models.CharField(
        '类别', max_length=20, choices=CATEGORY_CHOICES, default='other',
    )
    importance = models.IntegerField('重要度', default=5)
    source_message = models.ForeignKey(
        'chat.Message',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='extracted_memories',
        verbose_name='来源消息',
    )
    access_count = models.IntegerField('访问次数', default=0)
    last_accessed = models.DateTimeField('上次访问', null=True, blank=True)
    consolidated = models.BooleanField(
        '已被聚合', default=False,
        help_text='该条记忆已被 consolidate_memories 命令合成为画像，检索时跳过',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-importance', '-updated_at']
        verbose_name = '记忆'
        verbose_name_plural = '记忆'
        indexes = [
            models.Index(fields=['user', '-importance']),
            models.Index(fields=['user', 'category']),
        ]

    def __str__(self):
        cat_label = dict(self.CATEGORY_CHOICES).get(self.category, self.category)
        return f'({cat_label}) {self.content[:40]}'
