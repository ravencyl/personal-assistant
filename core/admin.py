from django.contrib import admin
from django.conf import settings
from django.utils.html import format_html

from taggit.models import Tag as TaggitTag

from .models import PushSchedule, Tag


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
    list_display = ['name', 'scope', 'color_preview', 'is_active', 'sort', 'created_at']
    list_filter = ['scope', 'is_active']
    list_editable = ['is_active', 'sort']
    search_fields = ['name']
    ordering = ['scope', 'sort', 'name']

    @admin.display(description='底色')
    def color_preview(self, obj):
        """列表页色块预览（activity 标签才有底色）"""
        if not obj.color:
            return '—'
        return format_html(
            '<span style="display:inline-block;width:14px;height:14px;border-radius:4px;'
            'background:{};vertical-align:-2px;border:1px solid rgba(0,0,0,.12)"></span>'
            ' <code style="font-size:11px">{}</code>', obj.color, obj.color)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # color 用原生取色器编辑（hex 直接存库），取色器值天然是 #rrggbb
        if db_field.name == 'color':
            kwargs['widget'] = admin.widgets.AdminTextInputWidget(attrs={'type': 'color'})
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(PushSchedule)
class PushScheduleAdmin(admin.ModelAdmin):
    """推送计划：内容类型 × 触发时间，即改即生效（下次扫描按新表取）"""
    list_display = ['user', 'push_type', 'time', 'enabled', 'last_sent_date']
    list_filter = ['enabled', 'push_type']
    list_editable = ['time', 'enabled']
    search_fields = ['user__username']
    ordering = ['time']


# 自定义 Admin 站点标题（品牌名统一取 settings.SITE_NAME）
admin.site.site_header = f'{settings.SITE_NAME} 管理后台'
admin.site.site_title = settings.SITE_NAME
admin.site.index_title = '管理面板'
