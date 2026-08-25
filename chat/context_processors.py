from .models import Conversation
from core.utils import visible_qs
from django.utils import timezone

# 浮窗中展示的最近对话数量
WIDGET_CONVERSATION_LIMIT = 8


def chat_widget(request):
    """为全站页面提供左下角聊天浮窗所需的最近对话列表（超级用户可见全部）"""
    if not request.user.is_authenticated:
        return {}

    conversations = visible_qs(
        Conversation, request.user,
    ).exclude(status='archived')[:WIDGET_CONVERSATION_LIMIT]

    # 到期未处理的提醒计数（用于浮窗红点）
    from core.models import Reminder
    pending_count = Reminder.objects.filter(
        user=request.user,
        status='pending',
        trigger_at__lte=timezone.now(),
    ).count()

    return {
        'widget_conversations': conversations,
        'pending_reminder_count': pending_count,
    }
