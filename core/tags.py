"""跨模块标签体系的唯一读写口径（自建 core.Tag，scope 区分用途）

历史：原用 django-taggit，Tag 全局一张表无用途字段、无预建能力、孤儿
标签无法清理；且不支持自定义 Tag 模型。2026-09 起改为 core.Tag +
各业务模型 M2M，本模块收拢全部读写：

- 可选项口径（autocomplete/筛选栏）：active_tag_names(scope) ∪ 用户
  在该 scope 用过的标签（含停用——编辑历史对象时回填不能丢）。
- 展示与统计口径：不过滤 is_active，停用标签的历史挂接照常显示。
- 写入单一入口 ensure_tags / apply_tags / add_tags：清洗（分隔符拆分、
  去重、限量）、同名停用标签直接复用（不复活也不重建）、新名字自动
  创建到对应 scope——跨 scope 的同名标签互不干扰。

core 不顶层依赖业务 app：模型映射在函数内延迟 import（与 cross_link
同模式）。
"""
import re

from django.db.models import Count

from .models import Tag

# 用户输入的分隔符口径（中文逗号/英文逗号/顿号），与历史 taggit 时代一致
_SPLIT_RE = re.compile(r'[,，、]')
MAX_TAGS_PER_OBJECT = 10

# scope → 业务模型 app 路径；M2M 反向查询名（related query name）单独列出，
# knowledge 的模型类是 Article，scope 名与反向名不同
_SCOPE_MODEL_PATHS = {
    'activity': 'activities.models.Activity',
    'expense': 'activities.models.Expense',
    'note': 'notes.models.Note',
    'knowledge': 'knowledge.models.Article',
}
_SCOPE_QUERY_NAMES = {
    'activity': 'activity',
    'expense': 'expense',
    'note': 'note',
    'knowledge': 'article',
}


def split_tag_names(raw):
    """标签入参清洗：数组或「a，b、c」字符串都接得下；去空去重限量"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = _SPLIT_RE.split(raw)
    else:
        parts = []
        for item in raw:
            parts.extend(_SPLIT_RE.split(str(item)))
    names, seen = [], set()
    for part in parts:
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
        if len(names) >= MAX_TAGS_PER_OBJECT:
            break
    return names


def ensure_tags(scope, names):
    """清洗入参并保证标签存在（scope 内 get_or_create），返回 Tag 实例列表

    - 同名标签已存在（无论启用与否）直接复用：停用标签被显式输入时不
      复活也不拒绝——挂接是中性的，是否重新启用由 admin 决定；
    - 新名字自动创建（is_active=True，sort 默认 100）。
    """
    from .models import Tag
    result = []
    for name in split_tag_names(names):
        tag, _created = Tag.objects.get_or_create(
            scope=scope, name=name, defaults={'is_active': True})
        result.append(tag)
    return result


def apply_tags(obj, names):
    """整体替换对象标签（create/update 的 set 语义）；空列表 = 清空"""
    obj.tags.set(ensure_tags(_scope_of(obj), names))


def add_tags(obj, names):
    """追加对象标签（add 语义：已有不动，只补新的）"""
    fresh = ensure_tags(_scope_of(obj), names)
    if fresh:
        obj.tags.add(*fresh)


def _scope_of(obj):
    """由对象类型反推 scope（调用方少传一个参数）"""
    for scope, model_path in _SCOPE_MODEL_PATHS.items():
        app, model_name = model_path.rsplit('.', 1)
        if type(obj).__name__ == model_name and type(obj).__module__.startswith(app):
            return scope
    raise ValueError(f'无法识别标签用途：{type(obj).__name__}（未注册的标签宿主模型）')


def tag_names(obj):
    """对象上的标签名列表（替代 taggit 的 .tags.names()）"""
    return list(obj.tags.values_list('name', flat=True))


def active_tag_names(scope):
    """预建且启用的标签名（autocomplete 的「建议」部分，按 sort 排序）"""
    return list(Tag.objects.filter(scope=scope, is_active=True)
                .order_by('sort', 'name').values_list('name', flat=True))


def used_tags(scope, user):
    """该用户在此 scope 的对象上用过的标签（Tag queryset，含停用）

    替代原 core.utils.used_tags：M2M 反向查询天然限定 scope，不再有
    taggit 时代「object_id 跨模型撞号必须手动限定 content_type」的坑。
    """
    from .utils import visible_qs
    model = _scope_model(scope)
    lookup = f'{_SCOPE_QUERY_NAMES[scope]}__in'
    return (Tag.objects.filter(**{lookup: visible_qs(model, user).values('id')})
            .distinct())


def used_tag_names(scope, user):
    """used_tags 的名字列表版本"""
    return list(used_tags(scope, user).order_by('name')
                .values_list('name', flat=True))


def tag_suggestions(scope, user):
    """autocomplete 合并源：预建启用标签在前（sort 序），用户用过的补后（去重）"""
    names = list(active_tag_names(scope))
    seen = set(names)
    for name in used_tag_names(scope, user):
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _scope_model(scope):
    """延迟 import 业务模型（core 不顶层依赖业务 app）"""
    from importlib import import_module
    module_path, model_name = _SCOPE_MODEL_PATHS[scope].rsplit('.', 1)
    return getattr(import_module(module_path), model_name)


# ── 智能标签建议 ──

# 建议标签数量上限（不超过 MAX_TAGS_PER_OBJECT）
SUGGEST_TAG_LIMIT = 5

# 名称相似度阈值（char_overlap_ratio）
_NAME_SIMILARITY_THRESHOLD = 0.5


def suggest_tags(obj, limit=SUGGEST_TAG_LIMIT, require_relevance=False, scope=None):
    """基于对象内容 + 用户历史标签习惯，推荐标签列表（top N）

    三层策略，按优先级叠加：
    1. 用户在该 scope 最常用的标签（频率排序）
    2. 对象名称与已有标签名的 char_overlap_ratio 匹配
    3. 同标签下其他对象的名称与当前对象名称的相似度

    require_relevance=True（AI 自动落库场景）时策略 1 不再无条件加分：
    常用标签与当前内容无关，无脑塞高频标签是「AI 乱贴标签」的主要来源；
    此时常用标签只有同时被策略 2/3 命中（内容相关）才会入选。
    给人工挑选的建议 chips（表单页）保持 require_relevance=False。

    失败返回空列表（不阻断创建/编辑流程）。
    """
    try:
        # scope 未传时由对象类型反推；临时对象（未落库预览）无法反推，需显式传
        scope = scope or _scope_of(obj)
        user = getattr(obj, 'user', None)
        if not user:
            return []

        # 获取对象的可搜索文本
        text = _get_object_text(obj)
        if not text:
            return []

        from core.utils import visible_qs, char_overlap_ratio

        candidates = {}  # tag_name → score

        model = _scope_model(scope)
        user_qs = visible_qs(model, user)

        # 策略 1：用户最常用的标签（频率排序，权重 3；自动落库场景跳过）
        if not require_relevance:
            freq_tags = (user_qs.values('tags__name')
                         .exclude(tags__name__isnull=True)
                         .annotate(cnt=Count('id'))
                         .order_by('-cnt')[:10])
            for row in freq_tags:
                name = row['tags__name']
                if name:
                    candidates[name] = candidates.get(name, 0) + 3

        # 策略 2：对象名称与标签名的相似度（权重 2）
        all_tags = list(Tag.objects.filter(scope=scope).values_list('name', flat=True))
        obj_name = getattr(obj, 'name', '') or getattr(obj, 'title', '') or ''
        if obj_name:
            for tag_name in all_tags:
                ratio = char_overlap_ratio(obj_name, tag_name, mode='contains')
                if ratio >= _NAME_SIMILARITY_THRESHOLD:
                    candidates[tag_name] = candidates.get(tag_name, 0) + 2

        # 策略 3：同标签下其他对象的名称相似度（权重 1）
        if obj_name and len(obj_name) >= 2:
            for tag_name in all_tags[:30]:  # 只检查前 30 个标签，避免查询过多
                similar_objs = (user_qs.filter(tags__name=tag_name)
                                .values_list('name', flat=True)[:5])
                for other_name in similar_objs:
                    if other_name and other_name != obj_name:
                        ratio = char_overlap_ratio(obj_name, other_name, mode='contains')
                        if ratio >= _NAME_SIMILARITY_THRESHOLD:
                            candidates[tag_name] = candidates.get(tag_name, 0) + 1
                            break  # 这个标签只要有一个相似对象就够了

        # 按分数排序，取 top N
        sorted_tags = sorted(candidates.items(), key=lambda x: -x[1])
        return [name for name, _score in sorted_tags[:limit]]

    except Exception:
        return []


def resolve_existing_tags(names, scope, user=None):
    """AI 自动识别路径的标签写入守门：只匹配已有标签，匹配不到的丢弃

    与 activities.resolve_participants 同口径——AI 推断不得自动新建标签，
    否则模型编造的名字会永久污染标签列表。合法池 = 预建启用标签 +
    该用户在此 scope 用过的标签（大小写不敏感，命中后落规范写法）。
    返回 (matched Tag 实例列表, skipped 原始名字列表)。
    """
    allowed = {}
    pool = list(active_tag_names(scope))
    if user:
        pool += list(used_tag_names(scope, user))
    for n in pool:
        if n:
            allowed.setdefault(n.strip().lower(), n.strip())

    matched, skipped, seen = [], [], set()
    for raw in names or []:
        name = str(raw).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in allowed:
            tag, _created = Tag.objects.get_or_create(
                scope=scope, name=allowed[key], defaults={'is_active': True})
            matched.append(tag)
        else:
            skipped.append(name)
    return matched, skipped


def _get_object_text(obj):
    """提取对象的可搜索文本（用于标签建议）"""
    parts = []
    name = getattr(obj, 'name', '') or getattr(obj, 'title', '') or ''
    if name:
        parts.append(name)
    desc = getattr(obj, 'description', '') or getattr(obj, 'content', '') or ''
    if desc:
        parts.append(desc[:200])
    return ' '.join(parts)
