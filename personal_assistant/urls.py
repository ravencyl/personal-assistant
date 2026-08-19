from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from wagtail.admin import urls as wagtailadmin_urls
from wagtail import urls as wagtail_urls
from wagtail.documents import urls as wagtaildocs_urls

from cms_pages.views import dashboard

urlpatterns = [
    # Dashboard (home)
    path('', dashboard, name='dashboard'),

    # Django admin (legacy, kept for compatibility)
    path('django-admin/', admin.site.urls),

    # Wagtail admin
    path('admin/', include(wagtailadmin_urls)),
    path('documents/', include(wagtaildocs_urls)),

    # Authentication
    path('accounts/', include('django.contrib.auth.urls')),

    # App modules
    path('chat/', include('chat.urls')),
    path('knowledge/', include('knowledge.urls')),
    path('tasks/', include('tasks.urls')),
    path('content/', include('content.urls')),

    # Agents API (internal)
    path('api/agents/', include('agents.urls')),

    # Wagtail catch-all (must be last)
    path('', include(wagtail_urls)),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
