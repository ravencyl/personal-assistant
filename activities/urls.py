from django.urls import path
from . import views

app_name = 'activities'

urlpatterns = [
    path('', views.activity_list, name='activity_list'),
    # /daily/ 简报页已下线（2026-09-19）：路由保留重定向回首页，name 不变让旧 url 标签不破
    path('daily/', views.daily_view, name='daily'),
    path('new/', views.activity_create, name='activity_create'),
    path('<int:activity_id>/', views.activity_detail, name='activity_detail'),
    path('<int:activity_id>/status/', views.activity_set_status, name='activity_set_status'),
    path('<int:activity_id>/pin/', views.activity_pin_toggle, name='activity_pin_toggle'),
    path('<int:activity_id>/edit/', views.activity_edit, name='activity_edit'),
    path('<int:activity_id>/subactivities/', views.add_subactivity, name='add_subactivity'),
    path('<int:activity_id>/subactivities/manual-create/', views.subactivity_manual_create,
         name='subactivity_manual_create'),
    path('<int:activity_id>/expenses/add/', views.expense_create, name='expense_create'),
    path('<int:activity_id>/delete/', views.activity_delete, name='activity_delete'),
    path('expenses/<int:expense_id>/edit/', views.expense_edit, name='expense_edit'),
    path('expenses/<int:expense_id>/delete/', views.expense_delete, name='expense_delete'),
    path('calendar/', views.activity_calendar, name='activity_calendar'),
    path('calendar/feed-settings/', views.calendar_feed_settings, name='calendar_feed_settings'),
    path('calendar-data/', views.calendar_data, name='calendar_data'),
    path('expense-chart-data/', views.expense_chart_data, name='expense_chart_data'),
    path('expense-heatmap-data/', views.expense_heatmap_data, name='expense_heatmap_data'),
    path('expense-report/', views.expense_report, name='expense_report'),
    path('export/activities/', views.activity_export_csv, name='activity_export'),
    path('export/expenses/', views.expense_export_csv, name='expense_export'),
    path('attachments/upload/<int:activity_id>/', views.attachment_upload, name='attachment_upload'),
    path('attachments/<int:attachment_id>/delete/', views.attachment_delete, name='attachment_delete'),
    path('next-actions/', views.next_actions, name='next_actions'),
    path('archive/', views.archive_list, name='archive_list'),
    path('<int:activity_id>/archive/', views.archive_activity, name='archive_activity'),
    path('<int:activity_id>/unarchive/', views.unarchive_activity, name='unarchive_activity'),
    path('templates/', views.template_list, name='template_list'),
    path('templates/create/', views.template_create, name='template_create'),
    path('templates/<int:template_id>/use/', views.template_use, name='template_use'),
    path('<int:pk>/move-date/', views.move_date_view, name='move_date'),
]
