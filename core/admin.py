from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django import forms

from .models import Reminder, DailySummary, SuggestionState, DailyInsight


# 自定义 Admin 站点标题
admin.site.site_header = 'Personal AI Assistant'
admin.site.site_title = 'AI 助手管理'
admin.site.index_title = '管理面板'


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ('user', 'content', 'trigger_at', 'status')
    list_filter = ('status',)


@admin.register(DailySummary)
class DailySummaryAdmin(admin.ModelAdmin):
    list_display = ('user', 'summary_date', 'status', 'generated_at')
    list_filter = ('status',)


@admin.register(SuggestionState)
class SuggestionStateAdmin(admin.ModelAdmin):
    list_display = ('user', 'fingerprint', 'action', 'created_at')
    list_filter = ('action',)


@admin.register(DailyInsight)
class DailyInsightAdmin(admin.ModelAdmin):
    list_display = ('user', 'insight_date', 'status', 'generated_at')
    list_filter = ('status',)
