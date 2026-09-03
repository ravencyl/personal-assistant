from .models import Conversation
from core.utils import pending_reminders, visible_qs

# 浮窗中展示的最近对话数量
WIDGET_CONVERSATION_LIMIT = 8


def chat_widget(request):
    """为全站页面提供右下角聊天浮窗所需的最近对话列表（超级用户可见全部）"""
    if not request.user.is_authenticated:
        return {}

    conversations = visible_qs(
        Conversation, request.user,
    ).exclude(status='archived')[:WIDGET_CONVERSATION_LIMIT]

    # 浮窗红点：直接用全站唯一的「待处理」口径（core.utils.pending_reminders），
    # 与 Daily「提醒」区、AI 的 list_reminders 是同一个数。以前这里自己写了一份
    # OR 查询（注释声称等于 Daily 区，其实 Daily 只取 fired，两者根本不等价），
    # 于是出现过：红点说有条待办、点进 Daily 什么都没有、问 AI 答「没有提醒」。
    # 本函数每页都跑，所以只读不写：不去调 check_due_reminders 补落库，
    # 口径本身已对「落没落库」不敏感。
    pending_count = pending_reminders(request.user).count()

    return {
        'widget_conversations': conversations,
        'pending_reminder_count': pending_count,
    }
