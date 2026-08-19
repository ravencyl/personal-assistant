from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from taggit.managers import TaggableManager


class KnowledgeCategory(models.Model):
    """知识分类"""
    name = models.CharField('名称', max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField('描述', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '知识分类'
        verbose_name_plural = '知识分类'
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True) or slugify(f'category-{self.pk or 0}')
        super().save(*args, **kwargs)


class ArticleQuerySet(models.QuerySet):
    def published(self):
        return self.filter(is_published=True)

    def search(self, query):
        """简单全文检索：匹配标题、正文、AI 摘要"""
        if not query:
            return self.none()
        return self.filter(
            Q(title__icontains=query)
            | Q(body__icontains=query)
            | Q(ai_summary__icontains=query)
        )


class KnowledgeArticle(models.Model):
    """知识文章"""
    title = models.CharField('标题', max_length=200)
    slug = models.SlugField(max_length=200, blank=True)
    category = models.ForeignKey(
        KnowledgeCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='articles',
        verbose_name='分类',
    )
    body = models.TextField('正文', help_text='支持 Markdown 格式')
    source_file = models.FileField('源文件', upload_to='knowledge/', blank=True)
    tags = TaggableManager(blank=True, verbose_name='标签')
    ai_summary = models.TextField('AI 摘要', blank=True, help_text='AI 生成的摘要')
    is_published = models.BooleanField('已发布', default=True)
    published_at = models.DateTimeField('发布时间', default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ArticleQuerySet.as_manager()

    class Meta:
        verbose_name = '知识文章'
        verbose_name_plural = '知识文章'
        ordering = ['-published_at']

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title, allow_unicode=True) or slugify(f'article-{timezone.now().timestamp():.0f}')
        super().save(*args, **kwargs)
