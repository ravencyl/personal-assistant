from django.urls import path
from . import views

app_name = 'activities'

urlpatterns = [
    path('', views.activity_list, name='activity_list'),
    path('new/', views.activity_create, name='activity_create'),
    path('parse-quick-input/', views.parse_quick_input_view, name='parse_quick_input'),
    path('quick-create/', views.activity_quick_create, name='activity_quick_create'),
    path('<int:activity_id>/', views.activity_detail, name='activity_detail'),
    path('<int:activity_id>/status/', views.activity_set_status, name='activity_set_status'),
    path('<int:activity_id>/edit/', views.activity_edit, name='activity_edit'),
    path('<int:activity_id>/subactivities/', views.add_subactivity, name='add_subactivity'),
    path('<int:activity_id>/quick-sub/', views.activity_quick_sub, name='activity_quick_sub'),
    path('<int:activity_id>/expenses/add/', views.expense_create, name='expense_create'),
    path('<int:activity_id>/delete/', views.activity_delete, name='activity_delete'),
    path('expenses/<int:expense_id>/delete/', views.expense_delete, name='expense_delete'),
    path('calendar/', views.activity_calendar, name='activity_calendar'),
    path('calendar-data/', views.calendar_data, name='calendar_data'),
]
