from django.contrib import admin
from django.conf import settings

from taggit.models import Tag as TaggitTag

from .models import Tag


# taggit 已被自建 core.Tag 取代（其数据表已由 activities.0016 删除），但
# app 注册必须保留——历史迁移的 dependencies 引用 taggit 迁移节点，移除会
# 断迁移图。这里把它自带的 admin 注册注销掉，避免管理后台出现指向已删表
# 的死入口（点进去直接 no such table 500）。
try:
    admin.site.unregister(TaggitTag)
except admin.sites.NotRegistered:
    pass


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
