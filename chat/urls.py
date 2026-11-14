from django.urls import path
from . import views

app_name = 'chat'

urlpatterns = [
    # 独立对话页 2026-11-13 下线（用户：「又有 AI 对话又有侧边栏 AI，不用单独的
    # AI 对话页面了」）。对话能力全部由全站侧栏（桌面）/ 浮窗弹窗（移动）/ 活动详情
    # 「问 AI」抽屉承担，后端端点（send/turn/sidebar/for-activity/…）全部保留。
    # 三条页面路由保留为重定向墓碑：name 不变让旧书签 / 模板 {% url %} / 推送深链
    # 不破，落点改首页 + ?chat=<id> 唤起侧栏并挂到该对话（无 JS 时落首页）。
    path('', views.chat_page_redirect, name='conversation_list'),
    path('<int:conversation_id>/', views.chat_page_redirect, name='conversation_list_with_active'),
    path('<int:conversation_id>/detail/', views.chat_page_redirect, name='conversation_detail'),
    # 周回顾深链（推送 / 周报入口，原 /chat/?ask=weekly_review）：建回顾对话后唤起侧栏
    path('weekly-review/', views.weekly_review_chat, name='weekly_review_chat'),
    path('<int:conversation_id>/widget-messages/', views.widget_messages, name='widget_messages'),
    path('create/', views.create_conversation, name='create_conversation'),
    # 活动专属对话 create-or-get：详情页「问 AI」抽屉入口（JSON，原生 fetch，禁 hx-*）
    path('for-activity/<int:activity_id>/', views.activity_conversation, name='activity_conversation'),
    # 全站侧边 AI 助手：常驻单会话 create-or-get（JSON，原生 fetch，禁 hx-*）
    path('sidebar/', views.sidebar_session, name='sidebar_session'),
    # 侧栏历史对话列表片段（JSON 包 HTML，原生 fetch，禁 hx-*）
    path('sidebar/conversations/', views.sidebar_conversations, name='sidebar_conversations'),
    path('<int:conversation_id>/send/', views.send_message, name='send_message'),
    # 非 AI 快捷卡片：点击 → 服务端直出（不碰 Qoder / turn 状态机，JSON 由原生 fetch 消费，禁挂 hx-*）
    path('<int:conversation_id>/local-card/', views.local_card_create, name='local_card_create'),
    # 异步收发：发送秒返回，结果靠轮询；两个端点都回 JSON，由原生 fetch 消费（禁止挂 hx-*）
    path('<int:conversation_id>/turn/', views.turn_poll, name='turn_poll'),
    # 长轮询收口：请求内挂 SSE 通知器等本轮结束（≤25s），收到信号走同一个收尾路径；同 JSON 口径
    path('<int:conversation_id>/turn-wait/', views.turn_wait, name='turn_wait'),
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
