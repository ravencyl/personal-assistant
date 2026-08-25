from django.urls import path
from . import views

app_name = 'notes'

urlpatterns = [
    path('', views.note_list, name='note_list'),
    path('create/', views.note_create, name='note_create'),
    path('<int:note_id>/edit/', views.note_edit, name='note_edit'),
    path('<int:note_id>/delete/', views.note_delete, name='note_delete'),
    path('<int:note_id>/pin/', views.note_toggle_pin, name='note_toggle_pin'),
]
