"""全局搜索引擎

跨 Activity / Article / Note / Conversation / Message / Memory 六个模块的统一搜索。
FTS5 可用时优先走全文索引（BM25 排序），不可用时静默降级为 icontains。
"""
import logging
from datetime import timedelta

from django.utils import timezone

from core.utils import visible_qs, visible_child_qs, q_or

logger = logging.getLogger(__name__)


def _try_fts(user, query, limit_per_module):
    """尝试 FTS5 搜索，返回 (fts_ids, used) 二元组。

    fts_ids: {'activity': [id,...], 'article': [id,...], 'note': [id,...]} 或 None
    used: 是否成功使用了 FTS（False 表示需降级）
    """
    from core.fts import fts_search
    fts_ids = fts_search(user, query, limit_per_module)
    if fts_ids is None:
        return None, False
    # 即使三个模块都为空列表，也算 FTS 生效（只是没匹配到）
    return fts_ids, True


def global_search(user, query, limit_per_module=5):
    """跨模块统一搜索，返回按模块分组的结果。

    返回格式: {
        'activities': [Activity, ...],
        'articles': [Article, ...],
        'notes': [Note, ...],
        'conversations': [Conversation, ...],
        'messages': [(Message, Conversation), ...],
        'memories': [Memory, ...],
    }
    """
    if not query or not query.strip():
        return {'activities': [], 'articles': [], 'notes': [],
                'conversations': [], 'messages': [], 'memories': []}

    q = query.strip()
    results = {}

    # 尝试 FTS5 加速（仅覆盖 activity / article / note 三个模块）
    fts_ids, fts_used = _try_fts(user, q, limit_per_module)

    # 活动：搜索名称、描述、标签
    from activities.models import Activity
    if fts_used and fts_ids.get('activity'):
        activities = visible_qs(Activity, user).filter(
            pk__in=fts_ids['activity']
        )[:limit_per_module]
    else:
        activities = visible_qs(Activity, user).filter(
            q_or(('name', 'description', 'tags__name'), q)
        ).distinct()[:limit_per_module]
    results['activities'] = list(activities)

    # 知识库：搜索标题、内容、标签
    from knowledge.models import Article
    if fts_used and fts_ids.get('article'):
        articles = visible_qs(Article, user).filter(
            pk__in=fts_ids['article']
        )[:limit_per_module]
    else:
        articles = visible_qs(Article, user).filter(
            q_or(('title', 'content', 'tags__name'), q)
        ).distinct()[:limit_per_module]
    results['articles'] = list(articles)

    # 笔记：搜索内容、标签
    from notes.models import Note
    if fts_used and fts_ids.get('note'):
        notes = visible_qs(Note, user).filter(
            pk__in=fts_ids['note']
        )[:limit_per_module]
    else:
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

    # 记忆：搜索内容（按用户可见性，与活动/文章同口径）
    from memory.models import Memory
    memories = visible_qs(Memory, user).filter(
        content__icontains=q
    )[:limit_per_module]
    results['memories'] = list(memories)

    return results
