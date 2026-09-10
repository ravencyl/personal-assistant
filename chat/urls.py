from django.urls import path
from . import views

app_name = 'chat'

urlpatterns = [
    path('', views.conversation_list, name='conversation_list'),
    # 分栏布局：列表页带当前选中对话（桌面端右栏 / 移动端聊天视图）
    path('<int:conversation_id>/', views.conversation_list, name='conversation_list_with_active'),
    # 独立详情页：无 JS 降级 + 浮窗「在新页面打开」目标
    path('<int:conversation_id>/detail/', views.conversation_detail, name='conversation_detail'),
    path('<int:conversation_id>/widget-messages/', views.widget_messages, name='widget_messages'),
    path('create/', views.create_conversation, name='create_conversation'),
    path('<int:conversation_id>/send/', views.send_message, name='send_message'),
    # 异步收发：发送秒返回，结果靠轮询；两个端点都回 JSON，由原生 fetch 消费（禁止挂 hx-*）
    path('<int:conversation_id>/turn/', views.turn_poll, name='turn_poll'),
    path('<int:conversation_id>/turn/cancel/', views.turn_cancel, name='turn_cancel'),
    # @ 钉选：会话级「正在讨论哪个活动」，两个端点都回 JSON（原生 fetch，禁挂 hx-*）
    path('<int:conversation_id>/pin/', views.pin_conversation, name='pin_conversation'),
    path('pin/search/', views.pin_candidates, name='pin_candidates'),
    path('messages/<int:message_id>/confirm/', views.confirm_action, name='confirm_action'),
    # 候选点选：在候选卡上直接选目标，服务端重放工具（回 HTML 片段，HTMX 消费）
    path('messages/<int:message_id>/pick/', views.pick_candidate, name='pick_candidate'),
    path('<int:conversation_id>/archive/', views.archive_conversation, name='archive_conversation'),
    path('<int:conversation_id>/rename/', views.conversation_rename, name='conversation_rename'),
    path('<int:conversation_id>/delete/', views.conversation_delete, name='conversation_delete'),
]
