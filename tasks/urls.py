from django.urls import path
from . import views

app_name = 'tasks'

urlpatterns = [
    path('', views.task_list, name='task_list'),
    path('calendar/', views.calendar_view, name='calendar'),
    path('create/', views.create_task, name='create_task'),
    path('ai-parse/', views.ai_parse_task, name='ai_parse_task'),
    path('<int:task_id>/', views.task_detail, name='task_detail'),
    path('<int:task_id>/update/', views.update_task, name='update_task'),
    path('<int:task_id>/complete/', views.complete_task, name='complete_task'),
    path('<int:task_id>/delete/', views.delete_task, name='delete_task'),
]
