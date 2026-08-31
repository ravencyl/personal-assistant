"""跨模块关联推荐引擎

基于标签交集 + 分词兜底的双层策略，在 Activity / Article / Note 之间
建立关联推荐。

get_related_content() 返回除源模型外的另外两个模块的推荐结果，
键名统一为模型名小写复数形式（'activities' / 'articles' / 'notes'）。
"""
import logging
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import models
from django.db.models import Count

from core.utils import visible_qs

logger = logging.getLogger(__name__)

# 三个可互相推荐的模型标识 → (app_label, model_name, 分词兜底搜索字段)
# 字段集合放在这里而不是在 _token_fallback 里按模型名分支判断，
# 避免同一份模型映射在本文件里被表达两遍。
_TARGETS = {
    'activities': ('activities', 'Activity', ('name', 'description')),
    'articles':   ('knowledge',  'Article', ('title', 'content')),
    'notes':      ('notes',      'Note',    ('content',)),
}


def _get_model(app_label, model_name):
    from django.apps import apps
    return apps.get_model(app_label, model_name)


def get_related_content(user, source_model, source_instance, limit=5):
    """基于标签交集 + 分词兜底，返回关联的跨模块内容。

    返回格式（不含源模型自身类别）:
    {
        'activities': [{'object': Activity, 'score': int}, ...],  # 源非 Activity 时存在
        'articles':   [{'object': Article,  'score': int}, ...],  # 源非 Article 时存在
        'notes':      [{'object': Note,     'score': int}, ...],  # 源非 Note 时存在
    }
    """
    source_key = source_model.__name__.lower()
    # Activity → 'activities'; Article → 'articles'; Note → 'notes'
    if source_key == 'activity':
        source_key = 'activities'
    elif source_key == 'article':
        source_key = 'articles'
    elif source_key == 'note':
        source_key = 'notes'

    cache_key = f'related_{source_key}_{source_instance.id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    source_tags = list(source_instance.tags.names())

    # 确定需要查询的目标类别（排除源模型自身）
    target_keys = [k for k in _TARGETS if k != source_key]

    result = {}
    for key in target_keys:
        app_label, model_name, _fields = _TARGETS[key]
        target_model = _get_model(app_label, model_name)
        target_qs = visible_qs(target_model, user)

        # 第一层：标签交集
        items = _tag_intersection_scores(
            source_tags, target_model, target_qs, limit=limit,
        )

        # 第二层：分词兜底（不足时）
        if len(items) < limit:
            text = _get_text_for_fallback(source_instance)
            if text:
                exclude_ids = {it['object'].id for it in items}
                items.extend(_token_fallback(
                    text, _TARGETS[key][2], target_qs,
                    exclude_ids=exclude_ids,
                    limit=limit - len(items),
                ))

        result[key] = items[:limit]

    cache.set(cache_key, result, timeout=1800)  # 30 分钟
    return result


def _tag_intersection_scores(source_tags, target_model, target_qs, limit=5):
    """计算目标模型中每个对象与源标签的交集数量，按交集大小排序"""
    if not source_tags:
        return []

    ct = ContentType.objects.get_for_model(target_model)
    from taggit.models import TaggedItem

    tagged = TaggedItem.objects.filter(
        content_type=ct,
        tag__name__in=source_tags,
    ).values('object_id').annotate(
        shared_count=Count('tag', distinct=True)
    ).filter(shared_count__gte=1).order_by('-shared_count')

    top_ids = [t['object_id'] for t in tagged[:limit * 2]]
    objects = {obj.id: obj for obj in target_qs.filter(id__in=top_ids)}

    results = []
    for t in tagged[:limit]:
        obj = objects.get(t['object_id'])
        if obj:
            results.append({'object': obj, 'score': t['shared_count']})
    return results


def _get_text_for_fallback(instance):
    """获取用于分词兜底的文本"""
    from activities.models import Activity
    from knowledge.models import Article
    from notes.models import Note

    if isinstance(instance, Activity):
        return f'{instance.name} {instance.description}'
    elif isinstance(instance, Article):
        return f'{instance.title} {instance.content[:200]}'
    elif isinstance(instance, Note):
        return instance.content[:200]
    return ''


def _token_fallback(text, search_fields, target_qs, exclude_ids=None, limit=5):
    """标签交集不足时，用分词 + icontains 模糊匹配补足（字段集由 _TARGETS 给定）"""
    from knowledge.utils import tokenize
    from core.utils import q_or

    tokens = tokenize(text, max_tokens=3)
    if not tokens:
        return []

    if exclude_ids:
        target_qs = target_qs.exclude(id__in=exclude_ids)

    q = models.Q()
    for token in tokens:
        q |= q_or(search_fields, token)

    return [{'object': obj, 'score': 1} for obj in target_qs.filter(q).distinct()[:limit]]


def invalidate_related_cache(sender, instance, **kwargs):
    """信号处理器：清除关联推荐缓存"""
    model_name = sender.__name__.lower()
    if model_name == 'activity':
        model_name = 'activities'
    elif model_name == 'article':
        model_name = 'articles'
    elif model_name == 'note':
        model_name = 'notes'

    # 清除以该实例为源时的缓存
    cache_key = f'related_{model_name}_{instance.id}'
    cache.delete(cache_key)

    # 同时清除其他模型以该实例为推荐目标时可能涉及的缓存（保守策略：清除所有 related_* 不现实，
    # 这里仅清除自身作为源的缓存；其他缓存会在 30 分钟后自然过期）
