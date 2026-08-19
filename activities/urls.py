from django.urls import path
from . import views

app_name = 'activities'

urlpatterns = [
    path('', views.activity_list, name='activity_list'),
    path('new/', views.activity_create, name='activity_create'),
    path('<int:activity_id>/', views.activity_detail, name='activity_detail'),
    path('<int:activity_id>/edit/', views.activity_edit, name='activity_edit'),
    path('<int:activity_id>/subactivities/', views.add_subactivity, name='add_subactivity'),
    path('<int:activity_id>/delete/', views.activity_delete, name='activity_delete'),
]
