"""跨模块关联推荐引擎

基于标签交集 + 分词兜底的双层策略，在 Activity / Article 之间
建立关联推荐。

get_related_content() 返回除源模型外的另外模块的推荐结果，
键名统一为模型名小写复数形式（'activities' / 'articles'）。
"""
import logging
from django.core.cache import cache
from django.db import models
from django.db.models import Count

from core.utils import visible_qs

logger = logging.getLogger(__name__)

# 可互相推荐的模型标识 → (app_label, model_name, 分词兜底搜索字段)
# 字段集合放在这里而不是在 _token_fallback 里按模型名分支判断，
# 避免同一份模型映射在本文件里被表达两遍。
_TARGETS = {
    'activities': ('activities', 'Activity', ('name', 'description')),
    'articles':   ('knowledge',  'Document', ('title', 'content')),
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
    }
    """
    source_key = source_model.__name__.lower()
    # Activity → 'activities'; Document（Memory 文章代理）→ 'articles'
    if source_key in ('activity',):
        source_key = 'activities'
    elif source_key in ('article', 'document', 'memory'):
        source_key = 'articles'

    cache_key = f'related_{source_key}_{source_instance.id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    from core.tags import tag_names
    source_tags = tag_names(source_instance)

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
    """计算目标模型中每个对象与源标签的交集数量，按交集大小排序

    自建 core.Tag 后所有标签宿主模型的 M2M 正向名统一为 tags，
    不再需要 taggit 时代按 content_type 限定多态表。
    """
    if not source_tags:
        return []

    tagged = (target_qs.filter(tags__name__in=source_tags)
              .annotate(shared_count=Count('tags', distinct=True))
              .order_by('-shared_count'))

    results = []
    for obj in tagged[:limit]:
        results.append({'object': obj, 'score': obj.shared_count})
    return results


def _get_text_for_fallback(instance):
    """获取用于分词兜底的文本"""
    from activities.models import Activity
    from knowledge.models import Document

    if isinstance(instance, Activity):
        return f'{instance.name} {instance.description}'
    elif isinstance(instance, Document):
        return f'{instance.title} {instance.content[:200]}'
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
    """信号处理器：清除关联推荐缓存

    Memory/Document 双 sender（proxy 分离）：只有 document（文章）参与
    关联推荐，fact 条没有任何相关缓存要清，直接跳过。"""
    if sender.__name__ == 'Memory':
        if getattr(instance, 'type', '') != 'document':
            return
        model_name = 'articles'
    else:
        model_name = sender.__name__.lower()
        if model_name == 'activity':
            model_name = 'activities'
        elif model_name in ('article', 'document'):
            model_name = 'articles'

    # 清除以该实例为源时的缓存
    cache_key = f'related_{model_name}_{instance.id}'
    cache.delete(cache_key)

    # 同时清除其他模型以该实例为推荐目标时可能涉及的缓存（保守策略：清除所有 related_* 不现实，
    # 这里仅清除自身作为源的缓存；其他缓存会在 30 分钟后自然过期）
