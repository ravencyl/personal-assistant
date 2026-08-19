from django.urls import path
from . import views

app_name = 'chat'

urlpatterns = [
    path('', views.conversation_list, name='conversation_list'),
    path('<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
    path('create/', views.create_conversation, name='create_conversation'),
    path('<int:conversation_id>/send/', views.send_message, name='send_message'),
    path('<int:conversation_id>/archive/', views.archive_conversation, name='archive_conversation'),
    path('<int:conversation_id>/stream/', views.message_stream, name='message_stream'),
]
