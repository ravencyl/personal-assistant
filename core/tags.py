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
