from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from core.views import (dashboard, search_api, weekly_report, monthly_report, yearly_report,
                        report_send_to_chat, service_worker,
                        push_subscribe, push_unsubscribe, push_test,
                        weekly_review_view, weekly_review_complete_view,
                        today_view, set_timezone, get_timezone)
from activities.views import daily_view, calendar_feed
from chat.views import chat_home

urlpatterns = [
    # 首页 = Agent 对话列表：对话是整个 app 的默认入口，
    # 打开即落在对话里，零选择步骤；daily 创建失败自动退回对话列表
    path('', chat_home, name='home'),

    # /daily/ 简报页已下线（2026-09-19）：daily 信息与操作收进对话里的 daily 简报卡；
    # 路由保留重定向回首页（= 对话列表），name 不变让旧书签/模板 url 标签不破
    path('daily/', daily_view, name='daily'),

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

    # Web Push（VAPID）：订阅增删 + 测试推送（static/js/push.js 消费）
    path('api/push/subscribe/', push_subscribe, name='push_subscribe'),
    path('api/push/unsubscribe/', push_unsubscribe, name='push_unsubscribe'),
    path('api/push/test/', push_test, name='push_test'),

    # Reports
    path('reports/weekly/', weekly_report, name='weekly_report'),
    path('reports/monthly/', monthly_report, name='monthly_report'),
    path('reports/yearly/', yearly_report, name='yearly_report'),
    path('reports/send-to-chat/', report_send_to_chat, name='report_send_to_chat'),

    # Weekly Review
    path('weekly-review/', weekly_review_view, name='weekly_review'),
    path('weekly-review/complete/', weekly_review_complete_view, name='weekly_review_complete'),

    # /today/ 工作台页已下线（2026-09-19，与 /daily/ 同口径）：信息收进对话里的工作台卡；
    # 路由保留重定向回首页，name 不变让旧书签/模板 url 标签不破
    path('today/', today_view, name='today'),

    # Timezone
    path('api/timezone/', get_timezone, name='get_timezone'),
    path('api/timezone/set/', set_timezone, name='set_timezone'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
