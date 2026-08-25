from django.db import models
from django.conf import settings
from taggit.managers import TaggableManager


class Note(models.Model):
    """备忘录/速记"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notes'
    )
    content = models.TextField('内容')
    tags = TaggableManager(blank=True, verbose_name='标签')
    pinned = models.BooleanField('置顶', default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-pinned', '-updated_at']
        verbose_name = '备忘录'
        verbose_name_plural = '备忘录'
        indexes = [
            models.Index(fields=['user', '-updated_at']),
        ]

    def __str__(self):
        # 取内容前 50 字符作为摘要
        return self.content[:50] if self.content else '空笔记'
