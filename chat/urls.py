from django.urls import path
from . import views

app_name = 'chat'

urlpatterns = [
    path('', views.conversation_list, name='conversation_list'),
    path('<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
    path('<int:conversation_id>/widget-messages/', views.widget_messages, name='widget_messages'),
    path('create/', views.create_conversation, name='create_conversation'),
    path('<int:conversation_id>/send/', views.send_message, name='send_message'),
    path('<int:conversation_id>/archive/', views.archive_conversation, name='archive_conversation'),
]
