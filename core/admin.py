from django.contrib import admin
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django import forms

from .models import SuggestionState


# 自定义 Admin 站点标题（品牌名统一取 settings.SITE_NAME）
admin.site.site_header = f'{settings.SITE_NAME} 管理后台'
admin.site.site_title = settings.SITE_NAME
admin.site.index_title = '管理面板'


@admin.register(SuggestionState)
class SuggestionStateAdmin(admin.ModelAdmin):
    list_display = ('user', 'fingerprint', 'action', 'created_at')
    list_filter = ('action',)
