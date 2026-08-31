from .models import Conversation
from core.utils import visible_qs
from django.db.models import Q
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

    # 浮窗红点：口径 = Daily「提醒」区里能看到的待处理条数
    # （到期还没触发的 pending + 今天已触发未处理的 fired；done/dismissed 已处理不计，
    #   几天前 fired 却从未处理的老提醒也不计，否则留下永远消不掉的红点）
    from core.models import Reminder
    now = timezone.now()
    pending_count = Reminder.objects.filter(user=request.user).filter(
        Q(status='pending', trigger_at__lte=now)
        | Q(status='fired', trigger_at__date=timezone.localdate())
    ).count()

    return {
        'widget_conversations': conversations,
        'pending_reminder_count': pending_count,
    }
