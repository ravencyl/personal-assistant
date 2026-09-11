from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from core.views import (dashboard, search_api, weekly_report, monthly_report, yearly_report,
                        report_send_to_chat, service_worker)
from activities.views import daily_view, calendar_feed

urlpatterns = [
    # 首页 = 每日简报
    path('', daily_view, name='home'),

    # Dashboard
    path('dashboard/', dashboard, name='dashboard'),

    # PWA：Service Worker 必须在站点根路径，否则它能控制的范围只有 /static/
    # （详见 core/views.py::service_worker）；文件本体仍在 static/sw.js
    path('sw.js', service_worker, name='service_worker'),

    # Django admin
    path('admin/', admin.site.urls),

    # Authentication
    path('accounts/', include('django.contrib.auth.urls')),

    # 日历订阅（ICS/webcal）：token 即鉴权，Apple 日历服务器代拉取无法带会话；
    # 顶级路径便于在 iPhone 上直接粘贴订阅 URL（管理页在 /activities/calendar/feed-settings/）
    path('calendar/feed/<str:token>.ics', calendar_feed, name='calendar_feed_ics'),

    # App modules
    path('chat/', include('chat.urls')),
    path('activities/', include('activities.urls')),
    path('notes/', include('notes.urls')),
    path('knowledge/', include('knowledge.urls')),
    path('memory/', include('memory.urls')),

    # Agents API (internal)
    path('api/agents/', include('agents.urls')),

    # Global search API
    path('api/search/', search_api, name='global_search'),

    # Reports
    path('reports/weekly/', weekly_report, name='weekly_report'),
    path('reports/monthly/', monthly_report, name='monthly_report'),
    path('reports/yearly/', yearly_report, name='yearly_report'),
    path('reports/send-to-chat/', report_send_to_chat, name='report_send_to_chat'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
