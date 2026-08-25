from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from cms_pages.views import dashboard
from activities.views import daily_view

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

    # Agents API (internal)
    path('api/agents/', include('agents.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
