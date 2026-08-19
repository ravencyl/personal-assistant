from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django import forms


# 自定义 Admin 站点标题
admin.site.site_header = 'Personal AI Assistant'
admin.site.site_title = 'AI 助手管理'
admin.site.index_title = '管理面板'
