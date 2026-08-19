from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from cms_pages.views import dashboard
from activities.views import activity_list

urlpatterns = [
    # 首页 = 活动记录列表
    path('', activity_list, name='home'),

    # Dashboard
    path('dashboard/', dashboard, name='dashboard'),

    # Django admin
    path('admin/', admin.site.urls),

    # Authentication
    path('accounts/', include('django.contrib.auth.urls')),

    # App modules
    path('chat/', include('chat.urls')),
    path('knowledge/', include('knowledge.urls')),
    path('activities/', include('activities.urls')),
    path('content/', include('content.urls')),

    # Agents API (internal)
    path('api/agents/', include('agents.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
