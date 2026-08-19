from django.db import models
from modelcluster.fields import ParentalKey
from modelcluster.contrib.taggit import ClusterTaggableManager
from taggit.models import TaggedItemBase

from wagtail.models import Page
from wagtail.fields import RichTextField
from wagtail.admin.panels import FieldPanel
from wagtail.search import index


class ArticleTag(TaggedItemBase):
    content_object = ParentalKey(
        'knowledge.KnowledgeArticle',
        on_delete=models.CASCADE,
        related_name='tagged_items'
    )


class KnowledgeIndexPage(Page):
    """知识库首页/分类页"""
    description = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel('description'),
    ]

    subpage_types = ['knowledge.KnowledgeCategoryPage', 'knowledge.KnowledgeArticle']

    def get_context(self, request):
        context = super().get_context(request)
        context['articles'] = KnowledgeArticle.objects.live().descendant_of(self).order_by('-first_published_at')
        return context


class KnowledgeCategoryPage(Page):
    """知识分类页"""
    description = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel('description'),
    ]

    subpage_types = ['knowledge.KnowledgeArticle']
    parent_page_types = ['knowledge.KnowledgeIndexPage']

    def get_context(self, request):
        context = super().get_context(request)
        context['articles'] = KnowledgeArticle.objects.live().child_of(self).order_by('-first_published_at')
        return context


class KnowledgeArticle(Page):
    """知识文章"""
    body = RichTextField()
    source_file = models.FileField(upload_to='knowledge/', blank=True)
    tags = ClusterTaggableManager(through=ArticleTag, blank=True)
    ai_summary = models.TextField(blank=True, help_text='AI 生成的摘要')

    content_panels = Page.content_panels + [
        FieldPanel('body'),
        FieldPanel('source_file'),
        FieldPanel('tags'),
    ]

    search_fields = Page.search_fields + [
        index.SearchField('body'),
        index.SearchField('ai_summary'),
        index.FilterField('tags'),
    ]

    parent_page_types = ['knowledge.KnowledgeIndexPage', 'knowledge.KnowledgeCategoryPage']

    class Meta:
        verbose_name = '知识文章'
        verbose_name_plural = '知识文章'
