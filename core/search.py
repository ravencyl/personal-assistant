"""全局搜索引擎

跨 Activity / Article / Note / Conversation / Message 五个模块的统一搜索。
"""
import logging
from datetime import timedelta

from django.utils import timezone

from core.utils import visible_qs, visible_child_qs, q_or

logger = logging.getLogger(__name__)


def global_search(user, query, limit_per_module=5):
    """跨模块统一搜索，返回按模块分组的结果。

    返回格式: {
        'activities': [Activity, ...],
        'articles': [Article, ...],
        'notes': [Note, ...],
        'conversations': [Conversation, ...],
        'messages': [(Message, Conversation), ...],
    }
    """
    if not query or not query.strip():
        return {'activities': [], 'articles': [], 'notes': [],
                'conversations': [], 'messages': []}

    q = query.strip()
    results = {}

    # 活动：搜索名称、描述、标签
    from activities.models import Activity
    activities = visible_qs(Activity, user).filter(
        q_or(('name', 'description', 'tags__name'), q)
    ).distinct()[:limit_per_module]
    results['activities'] = list(activities)

    # 知识库：搜索标题、内容、标签
    from knowledge.models import Article
    articles = visible_qs(Article, user).filter(
        q_or(('title', 'content', 'tags__name'), q)
    ).distinct()[:limit_per_module]
    results['articles'] = list(articles)

    # 笔记：搜索内容、标签
    from notes.models import Note
    notes = visible_qs(Note, user).filter(
        q_or(('content', 'tags__name'), q)
    ).distinct()[:limit_per_module]
    results['notes'] = list(notes)

    # 对话：搜索标题
    from chat.models import Conversation, Message
    visible_conversations = visible_qs(Conversation, user)
    conversations = visible_conversations.filter(
        title__icontains=q
    )[:limit_per_module]
    results['conversations'] = list(conversations)

    # 消息内容：搜索最近 30 天的消息（按会话可见范围，与对话标题搜索同口径）
    thirty_days_ago = timezone.now() - timedelta(days=30)
    messages = visible_child_qs(Message, user, 'conversation').filter(
        content__icontains=q,
        created_at__gte=thirty_days_ago,
    ).select_related('conversation')[:limit_per_module]
    results['messages'] = [(m, m.conversation) for m in messages]

    return results
