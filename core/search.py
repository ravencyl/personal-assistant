"""全局搜索引擎

跨 Activity / Article / Note / Conversation / Message 五个模块的统一搜索。
"""
import logging
from datetime import timedelta

from django.db import models
from django.utils import timezone

from core.utils import visible_qs

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
        models.Q(name__icontains=q) |
        models.Q(description__icontains=q) |
        models.Q(tags__name__icontains=q)
    ).distinct()[:limit_per_module]
    results['activities'] = list(activities)

    # 知识库：搜索标题、内容、标签
    from knowledge.models import Article
    articles = visible_qs(Article, user).filter(
        models.Q(title__icontains=q) |
        models.Q(content__icontains=q) |
        models.Q(tags__name__icontains=q)
    ).distinct()[:limit_per_module]
    results['articles'] = list(articles)

    # 笔记：搜索内容、标签
    from notes.models import Note
    notes = Note.objects.filter(user=user).filter(
        models.Q(content__icontains=q) |
        models.Q(tags__name__icontains=q)
    ).distinct()[:limit_per_module]
    results['notes'] = list(notes)

    # 对话：搜索标题
    from chat.models import Conversation, Message
    conversations = visible_qs(Conversation, user).filter(
        title__icontains=q
    )[:limit_per_module]
    results['conversations'] = list(conversations)

    # 消息内容：搜索最近 30 天的消息
    thirty_days_ago = timezone.now() - timedelta(days=30)
    messages = Message.objects.filter(
        conversation__user=user,
        content__icontains=q,
        created_at__gte=thirty_days_ago,
    ).select_related('conversation')[:limit_per_module]
    results['messages'] = [(m, m.conversation) for m in messages]

    return results
