from django.urls import path
from . import views

app_name = 'agents'

urlpatterns = [
    path('status/', views.api_status, name='api_status'),
    path('sync/', views.sync_agents, name='sync_agents'),
]
