from django.contrib import admin
from django.conf import settings

from .models import Tag


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    """标签统一管理：scope 分用途展示，停用仅影响建议列表，历史挂接照常显示"""
    list_display = ['name', 'scope', 'is_active', 'sort', 'created_at']
    list_filter = ['scope', 'is_active']
    list_editable = ['is_active', 'sort']
    search_fields = ['name']
    ordering = ['scope', 'sort', 'name']


# 自定义 Admin 站点标题（品牌名统一取 settings.SITE_NAME）
admin.site.site_header = f'{settings.SITE_NAME} 管理后台'
admin.site.site_title = settings.SITE_NAME
admin.site.index_title = '管理面板'
