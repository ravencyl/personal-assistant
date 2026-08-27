"""活动模块共用工具函数

供 views.py 与 agent_tools.py 共同引用，避免跨模块导入私有函数。
"""
import logging
import re
from datetime import date

from django.db import models
from django.utils import timezone

from core.utils import visible_qs
from .models import Activity, ActivityLog

logger = logging.getLogger(__name__)


# ==================== 「日常开支」归属桶 ====================
# 桶活动为系统常驻活动，用于承接无活动语境的费用；
# 识别方式：标题 == DAILY_BUCKET_NAME 且描述含 DAILY_BUCKET_MARKER（比普通用户同名活动更稳妥）。
DAILY_BUCKET_NAME = '日常开支'
DAILY_BUCKET_MARKER = '系统归属桶：无活动语境的费用记入此处'


def get_daily_bucket(user):
    """惰性创建/复用该用户的「日常开支」归属桶活动"""
    bucket, _created = Activity.objects.get_or_create(
        user=user,
        name=DAILY_BUCKET_NAME,
        defaults={
            'description': DAILY_BUCKET_MARKER,
            'status': 'in_progress',
            'start_date': None,
            'end_date': None,
        },
    )
    return bucket


def is_daily_bucket(activity):
    """判断活动是否为「日常开支」归属桶"""
    return (
        activity.name == DAILY_BUCKET_NAME
        and DAILY_BUCKET_MARKER in (activity.description or '')
    )


def exclude_daily_bucket(qs):
    """从活动查询集中排除归属桶（列表页/每日简报展示用；费用统计不排除）"""
    return qs.exclude(name=DAILY_BUCKET_NAME, description__contains=DAILY_BUCKET_MARKER)


def log_activity(user, activity, action, summary=''):
    """写入活动操作日志（失败仅告警，不影响主流程）"""
    try:
        ActivityLog.objects.create(
            user=user,
            activity=activity,
            activity_name=activity.name,
            action=action,
            summary=summary,
        )
    except Exception as e:
        logger.warning(f'活动日志写入失败: {e}')


def snapshot_activity(activity):
    """编辑前字段快照，用于生成变更摘要"""
    return {
        'name': activity.name,
        'description': activity.description,
        'start_date': activity.start_date,
        'end_date': activity.end_date,
        'status': activity.status,
        'parent': activity.parent,
        'tags': set(activity.tags.names()),
        'participants': set(activity.participants.values_list('name', flat=True)),
    }


_EDIT_FIELDS = [
    ('name', '名称'), ('description', '描述'),
    ('start_date', '开始日期'), ('end_date', '结束日期'),
    ('status', '状态'),
]


def fmt_field(field, value):
    """格式化字段值用于变更摘要展示"""
    if value in (None, ''):
        return '空'
    if field == 'status':
        return dict(Activity.STATUS_CHOICES).get(value, str(value))
    return str(value)


def diff_part(label, old_set, new_set):
    """对比集合差异，生成 +/- 描述文本"""
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    parts = []
    if added:
        parts.append('+' + '、'.join(added))
    if removed:
        parts.append('-' + '、'.join(removed))
    return f'{label}{" ".join(parts)}' if parts else ''


def edit_summary(old, activity):
    """对比编辑前后快照，生成变更摘要"""
    changes = []
    for field, label in _EDIT_FIELDS:
        new_v = getattr(activity, field)
        if old[field] != new_v:
            changes.append(f'{label} {fmt_field(field, old[field])} → {fmt_field(field, new_v)}')
    if old['parent'] != activity.parent:
        old_p = old['parent'].name if old['parent'] else '无'
        new_p = activity.parent.name if activity.parent else '无'
        changes.append(f'父活动 {old_p} → {new_p}')
    tag_diff = diff_part('标签 ', old['tags'], set(activity.tags.names()))
    if tag_diff:
        changes.append(tag_diff)
    p_diff = diff_part('参与者 ', old['participants'],
                       set(activity.participants.values_list('name', flat=True)))
    if p_diff:
        changes.append(p_diff)
    return '；'.join(changes)[:500]


def normalize_input(data, today):
    """清洗校验解析结果（AI 与规则输出共用），丢弃非法字段"""
    out = {}
    name = str(data.get('name') or '').strip()
    if name:
        out['name'] = name[:255]
    for key in ('start_date', 'end_date'):
        value = data.get(key)
        if value:
            try:
                out[key] = date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:
                pass
    if out.get('start_date') and out.get('end_date') and out['end_date'] < out['start_date']:
        out['start_date'], out['end_date'] = out['end_date'], out['start_date']
    cost = data.get('cost')
    if cost is not None and cost != '':
        try:
            cost = float(cost)
            if cost >= 0:
                out['cost'] = cost
        except (TypeError, ValueError):
            pass
    status = data.get('status')
    if status in dict(Activity.STATUS_CHOICES):
        out['status'] = status
    for key in ('tags', 'participants'):
        values = data.get(key)
        if isinstance(values, str):
            values = [v.strip() for v in re.split(r'[,，、]', values)]
        if isinstance(values, list):
            values = [str(v).strip() for v in values if str(v).strip()]
            if values:
                out[key] = values[:10]
    return out


def filter_activities(user, params):
    """按条件筛选活动，返回 queryset（列表视图与 Agent 查询工具共用）

    params 支持：status / tag / date_from / date_to / name /
    participant（参与者，模糊）/ keyword（名称/描述/标签跨字段模糊），非法值静默忽略。
    日期筛选与列表页一致：按活动开始日期是否落在区间内。
    """
    qs = visible_qs(Activity, user).prefetch_related('tags')
    status = params.get('status')
    if status in dict(Activity.STATUS_CHOICES):
        qs = qs.filter(status=status)
    tag = str(params.get('tag') or '').strip()
    if tag:
        qs = qs.filter(tags__name=tag)
    for key, lookup in (('date_from', 'gte'), ('date_to', 'lte')):
        value = str(params.get(key) or '').strip()[:10]
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                continue
            qs = qs.filter(**{f'start_date__{lookup}': value})
    name = str(params.get('name') or '').strip()
    if name:
        qs = qs.filter(name__icontains=name)
    participant = str(params.get('participant') or '').strip()
    if participant:
        qs = qs.filter(participants__name__icontains=participant).distinct()
    keyword = str(params.get('keyword') or '').strip()
    if keyword:
        qs = qs.filter(
            models.Q(name__icontains=keyword)
            | models.Q(description__icontains=keyword)
            | models.Q(tags__name__icontains=keyword)
        ).distinct()
    return qs


def budget_status(activity):
    """计算活动预算状态，返回 (ratio, level, label)

    ratio: float (0.0 ~ 1.0+)，已花费/预算
    level: str，'safe' | 'warning' | 'over' | None
    label: str，中文状态标签或 None
    """
    if not activity.budget:
        return (None, None, None)

    spent = float(activity.total_cost or 0)
    budget = float(activity.budget)
    if budget <= 0:
        return (None, None, None)

    ratio = spent / budget

    if ratio >= 1.0:
        return (ratio, 'over', '已超预算')
    elif ratio >= 0.8:
        return (ratio, 'warning', '接近预算')
    else:
        return (ratio, 'safe', '')
