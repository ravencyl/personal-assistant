import hashlib

from django.db import models
from django.conf import settings
from django.utils.text import slugify

from core.models import Tag


class Article(models.Model):
    """知识库文章（Markdown）"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='articles'
    )
    title = models.CharField('标题', max_length=255)
    slug = models.SlugField('URL 标识', max_length=255, unique=True, blank=True)
    content = models.TextField('内容（Markdown）')
    tags = models.ManyToManyField(Tag, blank=True, verbose_name='标签')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # QMind 云端镜像状态（knowledge.qmind_sync）：source_id 用于更新/删除时定位云端源，
    # sync_hash 判断内容是否变化，避免每次保存都白走一轮上传
    qmind_source_id = models.CharField('QMind 源 ID', max_length=64, blank=True, default='')
    qmind_sync_hash = models.CharField('QMind 同步指纹', max_length=32, blank=True, default='')

    class Meta:
        ordering = ['-updated_at']
        verbose_name = '知识库文章'
        verbose_name_plural = '知识库文章'
        indexes = [
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return self.title

    def sync_hash(self):
        """标题+内容的指纹，与 qmind_sync_hash 比对判断是否需要重新同步"""
        return hashlib.md5(f'{self.title}\n{self.content}'.encode('utf-8')).hexdigest()

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title, allow_unicode=True) or f'article-{self.pk or "new"}'
            slug = base_slug
            counter = 1
            while Article.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base_slug}-{counter}'
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)
