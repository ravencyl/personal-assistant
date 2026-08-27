from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from core.views import dashboard, search_api, weekly_report, monthly_report, yearly_report, report_send_to_chat
from activities.views import daily_view
from core.reminder_views import reminder_dismiss, reminder_done

urlpatterns = [
    # 首页 = 每日简报
    path('', daily_view, name='home'),

    # Dashboard
    path('dashboard/', dashboard, name='dashboard'),

    # Django admin
    path('admin/', admin.site.urls),

    # Authentication
    path('accounts/', include('django.contrib.auth.urls')),

    # App modules
    path('chat/', include('chat.urls')),
    path('activities/', include('activities.urls')),
    path('notes/', include('notes.urls')),
    path('knowledge/', include('knowledge.urls')),

    # Agents API (internal)
    path('api/agents/', include('agents.urls')),

    # Global search API
    path('api/search/', search_api, name='global_search'),

    # Reports
    path('reports/weekly/', weekly_report, name='weekly_report'),
    path('reports/monthly/', monthly_report, name='monthly_report'),
    path('reports/yearly/', yearly_report, name='yearly_report'),
    path('reports/send-to-chat/', report_send_to_chat, name='report_send_to_chat'),

    # Reminders
    path('reminders/<int:reminder_id>/dismiss/', reminder_dismiss, name='reminder_dismiss'),
    path('reminders/<int:reminder_id>/done/', reminder_done, name='reminder_done'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
