"""活动模块共用工具函数

供 views.py 与 agent_tools.py 共同引用，避免跨模块导入私有函数。
"""
import logging
import re
from datetime import date

from django.db import models
from django.db.models import Sum

from core.utils import visible_qs, q_or
from .models import Activity, ActivityLog, Expense, Participant

logger = logging.getLogger(__name__)


# ==================== 「日常开支」归属桶 ====================
# 桶活动为系统常驻活动，用于承接无活动语境的费用；
# 识别方式：标题 == DAILY_BUCKET_NAME 且描述含 DAILY_BUCKET_MARKER（比普通用户同名活动更稳妥）。
DAILY_BUCKET_NAME = '日常开支'
DAILY_BUCKET_MARKER = '系统归属桶：无活动语境的费用记入此处'


def daily_bucket_q():
    """归属桶的唯一识别条件（标题 + 描述里的 marker）

    取桶 / 内存判定 / 查询集排除三处必须共用这一个条件：以前取桶只按 name、
    排除按 name+marker，用户手工建一个同名活动就会被静默收养成系统桶，
    既出现在列表里又充当费用归属对象。
    """
    return models.Q(name=DAILY_BUCKET_NAME, description__contains=DAILY_BUCKET_MARKER)


def get_daily_bucket(user):
    """惰性创建/复用该用户的「日常开支」归属桶活动

    按 marker 查找而不是按 name get_or_create：命中不到就新建一条带 marker 的，
    用户自建的同名普通活动不会被当成系统桶。
    """
    bucket = Activity.objects.filter(user=user).filter(daily_bucket_q()).first()
    if bucket is not None:
        return bucket
    return Activity.objects.create(
        user=user,
        name=DAILY_BUCKET_NAME,
        description=DAILY_BUCKET_MARKER,
        status='in_progress',
        start_date=None,
        end_date=None,
    )


def is_daily_bucket(activity):
    """判断活动是否为「日常开支」归属桶（daily_bucket_q 的内存版同口径，由测试交叉校验）"""
    return (
        activity.name == DAILY_BUCKET_NAME
        and DAILY_BUCKET_MARKER in (activity.description or '')
    )


def exclude_daily_bucket(qs):
    """从活动查询集中排除归属桶（列表页/每日简报展示用；费用统计不排除）"""
    return qs.exclude(daily_bucket_q())


# ==================== 参与者解析 ====================

def resolve_participants(user, names, create_missing=False):
    """按姓名解析参与者对象（不区分大小写、忽略首尾空白与 @ 前缀）

    create_missing=False（AI 自动识别路径）：只填已存在的参与者，匹配不到就跳过，
    避免把「yyx」这类大小写变体建成新联系人。
    create_missing=True（用户手动填写路径）：先复用已有写法，确实没有才新建，
    因此手输 yyx 会归到已存在的 YYX 而不是新开一条。

    返回 (participants, skipped_names, created_names)。
    """
    wanted, seen = [], set()
    for raw in names or []:
        name = str(raw).strip().lstrip('@').strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            wanted.append(name)
    if not wanted:
        return [], [], []

    index = {}
    for p in Participant.objects.filter(user=user):
        index.setdefault(p.name.strip().lower(), p)

    participants, skipped, created = [], [], []
    for name in wanted:
        p = index.get(name.lower())
        if p is None and create_missing:
            p = Participant.objects.create(user=user, name=name)
            index[name.lower()] = p
            created.append(p.name)
        if p is None:
            skipped.append(name)
            continue
        if p not in participants:
            participants.append(p)
    return participants, skipped, created


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
    text = str(value)
    if field == 'description':
        # 描述可能很长：变更摘要/活动日志里只留头一段（折行压成空格），
        # 整段贴进日志会撑爆时间线，也能避免被当作 AI 回复全文输出
        flat = ' '.join(text.split())
        return flat if len(flat) <= 40 else f'{flat[:40]}…'
    return text


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


def fmt_duration(minutes):
    """耗时分钟数的人性化展示，如「2 小时 30 分钟」「45 分钟」；空值返回「—」（批次4C 耗时统计）"""
    if minutes in (None, ''):
        return '—'
    minutes = int(minutes)
    hours, m = divmod(minutes, 60)
    if hours and m:
        return f'{hours} 小时 {m} 分钟'
    if hours:
        return f'{hours} 小时'
    return f'{m} 分钟'


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
            cost = round(float(cost), 2)
            if cost >= 0:
                out['cost'] = cost
        except (TypeError, ValueError):
            pass
    # budget 是「预算上限」（写 Activity.budget），与 cost（记一笔支出）两回事，不能合并
    budget = data.get('budget')
    if budget is not None and budget != '':
        try:
            budget = round(float(budget), 2)
            if budget >= 0:
                out['budget'] = budget
        except (TypeError, ValueError):
            pass
    status = data.get('status')
    if status in dict(Activity.STATUS_CHOICES):
        out['status'] = status
    duration = data.get('duration_minutes')
    if duration not in (None, ''):
        try:
            duration = int(duration)
            if duration >= 0:
                out['duration_minutes'] = duration
        except (TypeError, ValueError):
            pass
    for key in ('tags', 'participants'):
        values = data.get(key)
        if isinstance(values, str):
            values = [v.strip() for v in re.split(r'[,，、]', values)]
        if isinstance(values, list):
            values = [str(v).strip() for v in values if str(v).strip()]
            if values:
                out[key] = values[:10]
    return out


# 活动筛选参数名（与 filter_activities 支持的键对齐；name 只给 Agent 查询工具用，不从 URL 读）
FILTER_PARAM_KEYS = ('status', 'tag', 'date_from', 'date_to', 'participant', 'keyword')


def get_filter_params(request):
    """从 URL 查询串读取活动筛选参数（列表页 / CSV / JSON 导出共用）

    只做取值 + trim，合法性（状态枚举、日期格式等）统一由 filter_activities 判定；
    排序、分页等展示类参数不属筛选，由各自视图自取。
    """
    return {key: (request.GET.get(key) or '').strip() for key in FILTER_PARAM_KEYS}


def expense_totals_map(activity_ids):
    """批量取每个活动的直接费用合计 {activity_id: Decimal}

    无费用的活动不在返回字典里，调用方用 `.get(id, 0) or 0` 兼容。
    列表页 / 导出 / attach_costs 以前各写一份同样的 groupby。
    """
    ids = list(activity_ids)
    if not ids:
        return {}
    return dict(
        Expense.objects.filter(activity_id__in=ids)
        .values_list('activity_id').annotate(total=Sum('amount'))
        .values_list('activity_id', 'total')
    )


def filter_activities(user, params):
    """按条件筛选活动，返回 queryset（列表视图与 Agent 查询工具共用）

    params 支持：status / tag / date_from / date_to / name /
    participant（参与者，模糊）/ keyword（名称/描述/参与者/标签跨字段模糊），非法值静默忽略。
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
        qs = qs.filter(q_or(('name', 'description', 'tags__name',
                             'participants__name'), keyword)).distinct()
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
