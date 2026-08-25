from django.db import models
from django.conf import settings
from django.utils.text import slugify
from taggit.managers import TaggableManager


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
    tags = TaggableManager(blank=True, verbose_name='标签')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = '知识库文章'
        verbose_name_plural = '知识库文章'
        indexes = [
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return self.title

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
