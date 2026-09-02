from django.urls import path
from . import views

app_name = 'chat'

urlpatterns = [
    path('', views.conversation_list, name='conversation_list'),
    path('<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
    path('<int:conversation_id>/widget-messages/', views.widget_messages, name='widget_messages'),
    path('create/', views.create_conversation, name='create_conversation'),
    path('<int:conversation_id>/send/', views.send_message, name='send_message'),
    # 异步收发：发送秒返回，结果靠轮询；两个端点都回 JSON，由原生 fetch 消费（禁止挂 hx-*）
    path('<int:conversation_id>/turn/', views.turn_poll, name='turn_poll'),
    path('<int:conversation_id>/turn/cancel/', views.turn_cancel, name='turn_cancel'),
    path('messages/<int:message_id>/confirm/', views.confirm_action, name='confirm_action'),
    path('<int:conversation_id>/archive/', views.archive_conversation, name='archive_conversation'),
]
