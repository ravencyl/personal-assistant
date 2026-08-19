from django.contrib import admin
from .models import KnowledgeCategory, KnowledgeArticle


@admin.register(KnowledgeCategory)
class KnowledgeCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(KnowledgeArticle)
class KnowledgeArticleAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'is_published', 'published_at']
    list_filter = ['is_published', 'category']
    search_fields = ['title', 'body', 'ai_summary']
    prepopulated_fields = {'slug': ('title',)}
