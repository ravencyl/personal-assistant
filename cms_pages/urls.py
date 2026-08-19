from django.urls import path
from . import views

app_name = 'cms_pages'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
]
