"""费用类别的唯一读取口径（数据库驱动，增删改停用零代码变更）

历史：类别清单原本硬编码在 Expense.CATEGORY_CHOICES，散落 services/views/
templates/agent_tools/report_generator 七八处，新增一个类别要改代码发版本
（实证：add_expense 的 params_hint 枚举漏了数码/健康，硬编码必然漂移）。
现在类别配置在 ExpenseCategory 表（admin 可管理），所有入口从本模块读取。

两套口径，用途严格区分（防止「表单选不到但统计漏算」的不一致）：
- active_category_choices()：启用的类别，按排序。用于一切「可选项」：
  表单下拉、快记浮窗、类别建议、Agent params_hint。
- category_label_map()：全部类别（含停用）key→label。用于一切「展示与
  统计」：get_category_display、报表、图表、卡片。停用类别的历史费用
  依旧正常显示与统计，只是不再出现在新记录的可选项里。

缓存：django cache（生产 Redis 跨 gunicorn worker 共享，开发 LocMem），
TTL 300s 兜底；admin 增删改时 invalidate_category_cache() 立即失效。
"""
import hashlib
import json

from django.core.cache import cache

# 缓存键带版本号：读取结构变化时手动 bump，避免旧结构缓存残留
_CACHE_KEY = 'expense_categories_v1'
_TTL_SECONDS = 300

# 类别表为空（极端情况：初始迁移未跑 / 数据被清空）时的兜底，
# 与 services.PARSED_EXPENSE_CATEGORY 同值，但不 import 它（避免依赖环）
_FALLBACK_KEY = 'other'


def _load_rows():
    """读全量类别行（含停用），按 sort 排序；表为空时返回空列表"""
    from .models import ExpenseCategory
    return list(ExpenseCategory.objects
                .values('key', 'label', 'sort', 'is_active')
                .order_by('sort', 'id'))


def category_rows():
    """全量类别行（缓存），[{'key','label','sort','is_active'}, ...]"""
    rows = cache.get(_CACHE_KEY)
    if rows is None:
        rows = _load_rows()
        cache.set(_CACHE_KEY, rows, _TTL_SECONDS)
    return rows


def invalidate_category_cache():
    """类别配置变更后调用（admin 的增/改/删都会触发）"""
    cache.delete(_CACHE_KEY)


def active_category_choices():
    """启用的类别 [(key, label), ...]，按 sort 排序——一切「可选项」的唯一来源"""
    return [(r['key'], r['label']) for r in category_rows() if r['is_active']]


def category_label_map():
    """全部类别（含停用）{key: label}——一切「展示与统计」的唯一来源"""
    return {r['key']: r['label'] for r in category_rows()}


def active_category_keys():
    return {r['key'] for r in category_rows() if r['is_active']}


def category_cache_token():
    """类别配置版本指纹：进 views 的用户级缓存 key，类别一变旧缓存自动失效。

    返回 md5 短哈希而非原始 JSON：缓存 key 含引号/冒号等特殊字符会触发
    CacheKeyWarning（memcached 后端直接报错），哈希后只有十六进制字符。
    """
    raw = json.dumps(category_rows(), ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:10]


def default_category_key(default=_FALLBACK_KEY):
    """无指定时的默认类别：default 仍启用则用之，否则排序第一个启用类别"""
    active = [r['key'] for r in category_rows() if r['is_active']]
    if default in active:
        return default
    return active[0] if active else _FALLBACK_KEY
