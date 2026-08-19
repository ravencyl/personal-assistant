from .models import Conversation
from core.utils import visible_qs

# 浮窗中展示的最近对话数量
WIDGET_CONVERSATION_LIMIT = 8


def chat_widget(request):
    """为全站页面提供左下角聊天浮窗所需的最近对话列表（超级用户可见全部）"""
    if not request.user.is_authenticated:
        return {}

    conversations = visible_qs(
        Conversation, request.user,
    ).exclude(status='archived')[:WIDGET_CONVERSATION_LIMIT]

    return {'widget_conversations': conversations}
