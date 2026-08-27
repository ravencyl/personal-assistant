from django.urls import path
from . import views

app_name = 'memory'

urlpatterns = [
    path('', views.memory_list, name='memory_list'),
    path('<int:memory_id>/edit/', views.memory_edit, name='memory_edit'),
    path('<int:memory_id>/delete/', views.memory_delete, name='memory_delete'),
]
