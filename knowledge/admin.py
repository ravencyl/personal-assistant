from django.contrib import admin
from .models import Document


@admin.register(Document)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ['title', 'user', 'updated_at']
    list_filter = ['updated_at']
    search_fields = ['title', 'content']
    prepopulated_fields = {'slug': ('title',)}
