from django.db import models
from django.conf import settings
from taggit.managers import TaggableManager


class ContentCategory(models.Model):
    """内容分类"""
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = '内容分类'
        verbose_name_plural = '内容分类'
        ordering = ['name']

    def __str__(self):
        return self.name


class Bookmark(models.Model):
    """书签/收藏"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='bookmarks'
    )
    url = models.URLField()
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    ai_summary = models.TextField(blank=True, help_text='AI 生成的摘要')
    favicon = models.URLField(blank=True)
    category = models.ForeignKey(
        ContentCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bookmarks'
    )
    tags = TaggableManager(blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '书签'
        verbose_name_plural = '书签'

    def __str__(self):
        return self.title


class RSSFeed(models.Model):
    """RSS 订阅源"""
    name = models.CharField(max_length=255)
    url = models.URLField()
    category = models.ForeignKey(
        ContentCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feeds'
    )
    last_fetched = models.DateTimeField(null=True, blank=True)
    auto_fetch = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'RSS 订阅'
        verbose_name_plural = 'RSS 订阅'

    def __str__(self):
        return self.name


class FeedItem(models.Model):
    """RSS 订阅条目"""
    feed = models.ForeignKey(RSSFeed, on_delete=models.CASCADE, related_name='items')
    title = models.CharField(max_length=255)
    url = models.URLField()
    description = models.TextField(blank=True)
    content = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    ai_summary = models.TextField(blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-published_at', '-created_at']
        verbose_name = '订阅条目'
        verbose_name_plural = '订阅条目'

    def __str__(self):
        return self.title
