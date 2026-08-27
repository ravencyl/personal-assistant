"""活动 Agent 工具集（P0：query/get/set_status/create；P1：update/delete/stats）

注册到 core.agent_registry，由对话编排器按意图分发调用。
约定：权限一律经 visible_qs / get_visible；写操作强制 log_activity；
目标不明确时抛 ToolError / CandidateToolError 让用户澄清，绝不猜测；
update/delete 为两步确认流：预览卡片 + 确认后执行 apply_*。
"""
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from core.agent_registry import CandidateToolError, ToolError, agent_tool
from core.utils import get_visible, visible_qs

from .models import Activity, Participant, Expense
from .utils import (edit_summary, filter_activities, fmt_duration, fmt_field,
                    get_daily_bucket, is_daily_bucket, log_activity,
                    normalize_input, snapshot_activity)

STATUS_LABELS = dict(Activity.STATUS_CHOICES)

# 卡片快捷按钮只展示可达的下一状态
NEXT_STATUS = {
    'planned': ['in_progress', 'cancelled'],
    'in_progress': ['done', 'cancelled'],
    'done': [],
    'cancelled': [],
}


def _child_summary(child):
    d = child.start_date or child.end_date
    return {
        'id': child.id,
        'name': child.name,
        'status': child.status,
        'date_label': d.strftime('%m-%d') if d else '',
        'detail_url': reverse('activities:activity_detail', args=[child.id]),
    }


def _activity_card_data(activity):
    """单活动卡片快照：字段全部可 JSON 序列化，历史消息回放不依赖实时查询"""
    return {
        'id': activity.id,
        'name': activity.name,
        'status': activity.status,
        'status_label': activity.get_status_display(),
        'next_status': [
            {'value': s, 'label': STATUS_LABELS[s]} for s in NEXT_STATUS.get(activity.status, [])
        ],
        'date_range': activity.date_range,
        'duration': activity.duration_display,
        'expense_total': float(activity.total_cost or 0),
        'description': activity.description,
        'tags': list(activity.tags.names()),
        'participants': list(activity.participants.values_list('name', flat=True)),
        'children': [_child_summary(c) for c in activity.children.all()[:6]],
        'children_count': activity.children.count(),
        'detail_url': reverse('activities:activity_detail', args=[activity.id]),
        'edit_url': reverse('activities:activity_edit', args=[activity.id]),
    }


def _resolve_single(user, target):
    """按名称关键词定位唯一活动；0 条报错，多条抛候选列表供用户辨认"""
    target = str(target or '').strip()
    if not target:
        raise ToolError('请告诉我目标活动的名称')
    qs = visible_qs(Activity, user).filter(name__icontains=target)
    count = qs.count()
    if count == 0:
        raise ToolError(f'没有找到名称包含「{target}」的活动')
    if count > 1:
        candidates = [{
            'id': a.id,
            'name': a.name,
            'status': a.status,
            'status_label': a.get_status_display(),
            'date_label': (a.start_date or a.end_date).strftime('%m-%d') if (a.start_date or a.end_date) else '未设定',
            'detail_url': reverse('activities:activity_detail', args=[a.id]),
        } for a in qs[:5]]
        raise CandidateToolError(
            f'匹配到 {count} 个活动，请告诉我具体是哪一个（也可以直接打开详情修改）',
            candidates)
    return qs.first()


def _resolve_by_id(user, target_id):
    """确认流执行阶段按 id 精确定位（预览时已锁定目标，避免二次匹配歧义）"""
    return get_visible(Activity, user, id=target_id)


@agent_tool('activities.query', '按条件查询活动列表',
            'participant（参与者姓名，“和某人一起的活动”用这个，模糊不区分大小写）、'
            'keyword（主题关键词，“旅游相关/吃饭的”这类用这个，跨名称描述标签模糊搜）、'
            'name（活动名称关键词）、status、tag（精确标签名，不确定时用 keyword 代替）、'
            'date_from、date_to（YYYY-MM-DD），均可选，可组合')
def tool_query(user, params):
    qs = filter_activities(user, params).order_by('-start_date', '-created_at')
    total = qs.count()

    link_params = {k: str(params[k]).strip()
                   for k in ('status', 'tag', 'date_from', 'date_to', 'participant', 'keyword')
                   if str(params.get(k) or '').strip()}
    list_qs = urlencode(link_params)

    if total == 0:
        conds = '、'.join(f'{k}={v}' for k, v in link_params.items()) or '该条件'
        return {'reply': f'没有找到符合条件的活动（{conds}）。换个关键词试试，或者告诉我名称和日期，我来创建一个？'}

    items = []
    activity_ids = []
    for a in qs[:5]:
        d = a.start_date or a.end_date
        items.append({
            'id': a.id,
            'name': a.name,
            'status': a.status,
            'status_label': a.get_status_display(),
            'date_label': d.strftime('%m-%d') if d else '未设定',
            'expense_total': float(a.total_cost or 0),
            'detail_url': reverse('activities:activity_detail', args=[a.id]),
        })
        activity_ids.append(a.id)

    return {
        'reply': f'共找到 {total} 个符合条件的活动' + ('，以下是最近的几个：' if items else ''),
        'card': 'activity_list',
        'activity_ids': activity_ids,
        'card_data': {'items': items, 'total': total},
        'list_url': reverse('activities:activity_list') + (f'?{list_qs}' if list_qs else ''),
    }


@agent_tool('activities.get', '查看某个活动的详情', 'target（目标活动名称关键词）')
def tool_get(user, params):
    activity = _resolve_single(user, params.get('target') or params.get('name'))
    return {
        'reply': f'这是活动「{activity.name}」的详情：',
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': _activity_card_data(activity),
    }


@agent_tool('activities.set_status', '修改指定活动的状态',
            'target（目标活动名称关键词）、status（planned/in_progress/done/cancelled）')
def tool_set_status(user, params):
    status = params.get('status')
    if status not in STATUS_LABELS:
        raise ToolError('目标状态无效，可选：计划 / 进行中 / 已完成 / 已取消')
    activity = _resolve_single(user, params.get('target') or params.get('name'))
    if activity.status == status:
        return {
            'reply': f'「{activity.name}」已经处于「{STATUS_LABELS[status]}」状态了',
            'card': 'activity',
            'activity_ids': [activity.id],
            'card_data': _activity_card_data(activity),
        }
    old_label = STATUS_LABELS[activity.status]
    activity.status = status
    activity.save(update_fields=['status', 'updated_at'])
    log_activity(user, activity, 'status_changed',
                 f'状态「{old_label}」→「{STATUS_LABELS[status]}」（通过 AI 对话）')
    return {
        'reply': f'已将「{activity.name}」的状态从「{old_label}」改为「{STATUS_LABELS[status]}」',
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': _activity_card_data(activity),
        'changed': True,
    }


@agent_tool('activities.create', '创建一个新活动',
            'name（必填）、start_date/end_date（YYYY-MM-DD）、cost（数字，元，将创建为费用条目）、'
            'status、tags（字符串数组）、participants（字符串数组）、parent（父活动名称，可选）')
def tool_create(user, params):
    data = normalize_input(params, timezone.localdate())
    if not data.get('name'):
        raise ToolError('未能识别出活动名称，请写得更具体些')

    parent = None
    parent_name = str(params.get('parent') or '').strip()
    if parent_name:
        parent = visible_qs(Activity, user).filter(name__icontains=parent_name).first()

    cost_value = data.pop('cost', 0)
    activity = Activity.objects.create(
        user=user,
        name=data['name'],
        start_date=data.get('start_date'),
        end_date=data.get('end_date'),
        status=data.get('status', 'planned'),
        parent=parent,
    )
    if cost_value and float(cost_value) > 0:
        Expense.objects.create(
            activity=activity, user=user, amount=cost_value,
            category='other', note='通过 AI 对话创建',
        )
    if data.get('tags'):
        activity.tags.add(*data['tags'])
    if data.get('participants'):
        participants = [
            Participant.objects.get_or_create(user=user, name=name)[0]
            for name in data['participants']
        ]
        activity.participants.set(participants)
    log_activity(user, activity, 'created', '通过 AI 对话创建')

    suffix = f'，归属于「{parent.name}」' if parent else ''
    return {
        'reply': f'已创建活动「{activity.name}」（{activity.date_range}）{suffix}',
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': _activity_card_data(activity),
        'changed': True,
        'created': True,
    }


# ==================== P1：两步确认流（update / delete）====================

_UPDATE_FIELD_LABELS = [
    ('name', '名称'), ('start_date', '开始日期'), ('end_date', '结束日期'),
    ('status', '状态'), ('duration_minutes', '耗时（分钟）'),
]


def _update_preview(user, params):
    """预览阶段：定位目标 + 清洗参数 + 生成变更 diff（不写库）"""
    activity = _resolve_single(user, params.get('target') or params.get('name'))
    # name 在协议中兼任定位关键词：与目标名一致时不作为改名
    p = dict(params)
    if str(p.get('name') or '').strip() == activity.name:
        p.pop('name', None)
    data = normalize_input(p, timezone.localdate())

    changes = []
    for field, label in _UPDATE_FIELD_LABELS:
        if field not in data:
            continue
        old_v = getattr(activity, field)
        if field in ('start_date', 'end_date'):
            if (old_v.isoformat() if old_v else None) == data[field]:
                continue
        elif str(old_v or '') == str(data[field]):
            continue
        if field == 'duration_minutes':
            # 人性化格式展示，如「1 小时 30 分钟」
            changes.append({'field': field, 'label': label,
                            'old': fmt_duration(old_v), 'new': fmt_duration(data[field])})
            continue
        changes.append({'field': field, 'label': label,
                        'old': fmt_field(field, old_v), 'new': fmt_field(field, data[field])})

    for key, label in (('tags', '标签'), ('participants', '参与者')):
        if key in data:
            old_set = set(activity.tags.names()) if key == 'tags' else \
                set(activity.participants.values_list('name', flat=True))
            new_set = set(data[key])
            if old_set != new_set:
                added = sorted(new_set - old_set)
                removed = sorted(old_set - new_set)
                new_desc = ('、'.join(sorted(new_set))) or '清空'
                detail = []
                if added:
                    detail.append('+' + '、'.join(added))
                if removed:
                    detail.append('-' + '、'.join(removed))
                changes.append({'field': key, 'label': label,
                                'old': '、'.join(sorted(old_set)) or '空',
                                'new': new_desc + f"（{' '.join(detail)}）"})
    return activity, data, changes


def apply_update(user, params):
    """确认后执行：应用变更字段 + 日志记录 diff（通过 AI 对话）"""
    activity = _resolve_by_id(user, params.get('target_id'))
    p = dict(params)
    p.pop('target', None)
    if str(p.get('name') or '').strip() == activity.name:
        p.pop('name', None)
    data = normalize_input(p, timezone.localdate())

    old = snapshot_activity(activity)
    old_duration = activity.duration_minutes
    for field in ('name', 'start_date', 'end_date', 'status', 'duration_minutes'):
        if field in data:
            setattr(activity, field, data[field])
    activity.save()
    if 'tags' in data:
        activity.tags.set(*data['tags'])
    if 'participants' in data:
        participants = [
            Participant.objects.get_or_create(user=user, name=name)[0]
            for name in data['participants']
        ]
        activity.participants.set(participants)

    summary = edit_summary(old, activity)
    if activity.duration_minutes != old_duration:
        duration_change = (f'耗时 {fmt_duration(old_duration)} → {fmt_duration(activity.duration_minutes)}')
        summary = f'{summary}；{duration_change}' if summary else duration_change
    summary = summary or '无实质变更'
    log_activity(user, activity, 'edited', f'{summary}（通过 AI 对话）')
    return {
        'reply': f'已更新「{activity.name}」：{summary}',
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': _activity_card_data(activity),
        'changed': True,
    }


@agent_tool('activities.update', '修改指定活动的字段（名称/日期/费用/状态/标签/参与者/耗时）',
            'target（目标活动名称关键词）+ 要修改的字段（同 create 参数，另支持 duration_minutes 耗时分钟数）；先出预览，用户确认后生效',
            apply_fn=apply_update)
def tool_update(user, params):
    activity, data, changes = _update_preview(user, params)
    if not changes:
        return {
            'reply': f'没有识别到「{activity.name}」需要修改的内容，请告诉我要改哪些字段',
            'card': 'activity',
            'activity_ids': [activity.id],
            'card_data': _activity_card_data(activity),
        }
    return {
        'reply': f'我准备对「{activity.name}」做以下修改，请确认：',
        'card': 'confirm',
        'activity_ids': [activity.id],
        'card_data': {'kind': 'update', 'name': activity.name,
                      'detail_url': reverse('activities:activity_detail', args=[activity.id]),
                      'changes': changes},
        'action': {'tool': 'activities.update',
                   'params': {**params, 'target_id': activity.id}},
    }


def apply_delete(user, params):
    """确认后执行删除（子活动的父活动被清空，日志留存）"""
    activity = _resolve_by_id(user, params.get('target_id'))
    name = activity.name
    log_activity(user, activity, 'deleted', '通过 AI 对话删除')
    activity.delete()
    return {'reply': f'已删除活动「{name}」', 'changed': True}


@agent_tool('activities.delete', '删除指定活动（高危，必须先确认）',
            'target（目标活动名称关键词）；先出红色警示预览，用户确认后才删除',
            apply_fn=apply_delete)
def tool_delete(user, params):
    activity = _resolve_single(user, params.get('target') or params.get('name'))
    children_count = activity.children.count()
    return {
        'reply': f'即将删除活动「{activity.name}」，请确认：',
        'card': 'confirm',
        'activity_ids': [activity.id],
        'card_data': {'kind': 'delete', 'name': activity.name,
                      'date_range': activity.date_range,
                      'children_count': children_count,
                      'detail_url': reverse('activities:activity_detail', args=[activity.id])},
        'action': {'tool': 'activities.delete',
                   'params': {'target_id': activity.id}},
    }


# ==================== P1：统计 ====================

@agent_tool('activities.stats', '统计活动概况（状态分布/近 6 月趋势/热门标签/费用汇总）',
            '无必填参数；可选 scope：all（默认）/month（仅本月开始的活动）')
def tool_stats(user, params):
    qs = visible_qs(Activity, user)
    today = timezone.localdate()
    if str(params.get('scope') or '').strip() == 'month':
        qs = qs.filter(start_date__year=today.year, start_date__month=today.month)

    total = qs.count()
    if total == 0:
        return {'reply': '目前还没有活动记录，要不要先创建一个？'}

    # 状态分布（按固定顺序，模板画分段条）
    status_counts = dict(qs.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))
    status_dist = [{'status': s, 'label': STATUS_LABELS[s], 'count': status_counts.get(s, 0),
                    'pct': round(status_counts.get(s, 0) * 100 / total)}
                   for s in ('planned', 'in_progress', 'done', 'cancelled')]

    # 近 6 月柱状（按开始日期所在月）
    months = []
    y, m = today.year, today.month
    for _ in range(6):
        months.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    months.reverse()
    month_qs = qs.filter(start_date__isnull=False)
    month_counts = {}
    for d in month_qs.values('start_date').annotate(n=Count('id')):
        key = (d['start_date'].year, d['start_date'].month)
        month_counts[key] = month_counts.get(key, 0) + d['n']
    month_bars = [{'label': f'{m}月', 'count': month_counts.get((y, m), 0)} for y, m in months]
    max_month = max((b['count'] for b in month_bars), default=0) or 1
    for b in month_bars:
        b['pct'] = round(b['count'] * 100 / max_month)

    # 热门标签 Top 5
    tag_rows = list(qs.values('tags__name').annotate(n=Count('id'))
                    .filter(tags__name__isnull=False).order_by('-n')[:5])
    tags_top = [{'name': r['tags__name'], 'count': r['n']} for r in tag_rows]

    cost_total = qs.aggregate(s=Sum('expenses__amount'))['s'] or 0
    # 时间花费：用户全部活动耗时汇总（一条聚合查询），人性化格式输出；受 scope 限制同 qs 口径
    duration_total_minutes = visible_qs(Activity, user).aggregate(s=Sum('duration_minutes'))['s'] or 0

    return {
        'reply': f'共 {total} 个活动，概况如下：',
        'card': 'stats',
        'activity_ids': [],
        'card_data': {
            'total': total,
            'status_dist': status_dist,
            'month_bars': month_bars,
            'tags_top': tags_top,
            'cost_total': float(cost_total),
            'duration_total': fmt_duration(duration_total_minutes) if duration_total_minutes else '',
            'list_url': reverse('activities:activity_list'),
        },
    }


# ==================== P2：费用工具 ====================

_CATEGORY_MAP = {v: k for k, v in dict(Expense.CATEGORY_CHOICES).items()}

_NOTE_TOKEN_RE = re.compile(r'[\s,，。、;；:：!！?？()（）\[\]【】"\'~～·]+')


def _auto_expense_target(user, note):
    """target 缺省时的费用归属链：
    ① 当日/昨日日期重叠且进行中的唯一活动（多条不选）
    ② note 关键词对进行中活动标题 icontains 唯一命中（多条不选）
    ③ 「日常开支」归属桶兜底；返回 (activity, reason)
    """
    base = visible_qs(Activity, user).filter(status='in_progress')

    # ① 日期重叠：活动区间覆盖今天或昨天（无开始日期的桶活动自然不命中）
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    date_qs = base.filter(
        start_date__isnull=False,
        start_date__lte=today,
    ).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=yesterday)
    )
    if date_qs.count() == 1:
        return date_qs.first(), 'date'

    # ② note 分词后对进行中活动标题模糊匹配（跳过纯数字/金额词，唯一命中才用）
    tokens = [
        t for t in _NOTE_TOKEN_RE.split(str(note or ''))
        if len(t) >= 2 and not t.isdigit() and not re.fullmatch(r'[\d.]+[元块角分]?', t)
    ]
    if tokens:
        q = Q()
        for t in tokens:
            q |= Q(name__icontains=t)
        kw_qs = base.filter(q).distinct()
        kw_qs = kw_qs.exclude(pk__in=[a.pk for a in kw_qs if is_daily_bucket(a)])
        if kw_qs.count() == 1:
            return kw_qs.first(), 'keyword'

    # ③ 兜底归属桶
    return get_daily_bucket(user), 'bucket'


@agent_tool('activities.add_expense', '为活动添加一笔费用（目标可省略，自动归属）',
            'target（活动名称关键词，可省略：省略时依次尝试当日/昨日进行中的唯一活动、'
            'note 关键词唯一命中的进行中活动，都没有则记入「日常开支」）+ '
            'amount（金额，必填）+ '
            'category（类别：交通/住宿/餐饮/门票/购物/工作/其他）+ '
            'note（备注，可选，也参与归属匹配）+ paid_at（消费日期 YYYY-MM-DD，可选；'
            '相对日期需换算：“今天”用当前日期，“昨天”用当前日期减一天）')
def tool_add_expense(user, params):
    target = str(params.get('target') or params.get('name') or '').strip()
    if target:
        # 有 target：行为与原来完全一致（0 条报错，多条抛候选）
        activity = _resolve_single(user, target)
        reason = 'target'
    else:
        activity, reason = _auto_expense_target(user, params.get('note'))
    amount = params.get('amount')
    if amount is None:
        raise ToolError('请告诉我费用金额')
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ToolError('金额格式不正确，请输入数字')
    if amount <= 0:
        raise ToolError('金额必须大于 0')

    cat_input = str(params.get('category') or '其他').strip()
    category = _CATEGORY_MAP.get(cat_input, 'other')
    if cat_input in dict(Expense.CATEGORY_CHOICES):
        category = cat_input

    paid_at = params.get('paid_at')
    if paid_at:
        try:
            date.fromisoformat(str(paid_at)[:10])
            paid_at = str(paid_at)[:10]
        except ValueError:
            paid_at = None
    else:
        paid_at = None

    note = str(params.get('note') or '').strip()[:255]

    expense = Expense.objects.create(
        activity=activity, user=user, amount=amount,
        category=category, paid_at=paid_at, note=note,
    )
    log_activity(user, activity, 'edited',
                 f'添加费用 ¥{amount} [{expense.get_category_display()}]{" " + note if note else ""}（通过 AI 对话）')

    if reason == 'target':
        reply = f'已为「{activity.name}」添加费用 ¥{amount}（{expense.get_category_display()}）'
    elif reason == 'date':
        reply = f'已自动归入当日进行中的活动「{activity.name}」，添加费用 ¥{amount}（{expense.get_category_display()}）'
    elif reason == 'keyword':
        reply = f'已根据备注匹配到活动「{activity.name}」，添加费用 ¥{amount}（{expense.get_category_display()}）'
    else:
        reply = f'未找到明确归属的活动，已记入「{activity.name}」：费用 ¥{amount}（{expense.get_category_display()}）'

    return {
        'reply': reply,
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': _activity_card_data(activity),
        'changed': True,
    }


@agent_tool('activities.list_expenses', '查看某活动的费用明细',
            'target（活动名称关键词）')
def tool_list_expenses(user, params):
    activity = _resolve_single(user, params.get('target') or params.get('name'))
    expenses = list(activity.expenses.all())
    total = sum(float(e.amount) for e in expenses)

    if not expenses:
        return {
            'reply': f'「{activity.name}」还没有费用记录',
            'card': 'activity',
            'activity_ids': [activity.id],
            'card_data': _activity_card_data(activity),
        }

    items = []
    for e in expenses:
        items.append({
            'amount': float(e.amount),
            'category': e.get_category_display(),
            'note': e.note,
            'paid_at': e.paid_at.isoformat() if e.paid_at else '',
        })

    return {
        'reply': f'「{activity.name}」共 {len(expenses)} 笔费用，合计 ¥{total}：',
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': {**_activity_card_data(activity), 'expense_items': items},
    }


# ==================== P2：AA 分账 ====================

def apply_split_expense(user, params):
    """确认后执行：将总金额 AA 分给所有参与者，每人生成一笔费用"""
    activity = _resolve_by_id(user, params.get('target_id'))
    amount = float(params['amount'])
    per_person = float(params['per_person'])
    category = params.get('category', 'other')
    note = params.get('note', 'AA 分账')

    participants = list(activity.participants.all())
    for p in participants:
        Expense.objects.create(
            activity=activity, user=user, amount=per_person,
            category=category, note=f'{note}（{p.name}）',
        )
    log_activity(user, activity, 'edited',
                 f'AA 分账 ¥{amount} → {len(participants)} 人，每人 ¥{per_person}（通过 AI 对话）')

    return {
        'reply': f'已将 ¥{amount} 分给 {len(participants)} 人（每人 ¥{per_person}）',
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': _activity_card_data(activity),
        'changed': True,
    }


@agent_tool('activities.split_expense', '将活动的一笔费用 AA 分给所有参与者',
            'target（活动名称关键词）+ amount（总金额，必填）+ category（类别，可选）+ note（备注，可选）',
            apply_fn=apply_split_expense)
def tool_split_expense(user, params):
    activity = _resolve_single(user, params.get('target') or params.get('name'))
    amount = params.get('amount')
    if amount is None:
        raise ToolError('请告诉我费用总金额')
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ToolError('金额格式不正确')
    if amount <= 0:
        raise ToolError('金额必须大于 0')

    participants = list(activity.participants.all())
    if not participants:
        raise ToolError(f'「{activity.name}」还没有参与者，无法 AA 分账')

    per_person = round(amount / len(participants), 2)
    cat_input = str(params.get('category') or '其他').strip()
    category = _CATEGORY_MAP.get(cat_input, 'other')
    if cat_input in dict(Expense.CATEGORY_CHOICES):
        category = cat_input
    note = str(params.get('note') or 'AA 分账').strip()[:255]

    return {
        'reply': f'准备将 ¥{amount} 分给 {len(participants)} 位参与者（每人 ¥{per_person}），确认吗？',
        'card': 'confirm',
        'activity_ids': [activity.id],
        'card_data': {
            'kind': 'split_expense',
            'name': activity.name,
            'amount': amount,
            'per_person': per_person,
            'participant_count': len(participants),
            'participants': [p.name for p in participants],
            'detail_url': reverse('activities:activity_detail', args=[activity.id]),
        },
        'action': {
            'tool': 'activities.split_expense',
            'params': {**params, 'target_id': activity.id, 'per_person': per_person,
                       'category': category, 'note': note},
        },
    }


# ==================== P2：推迟/提前活动日期 ====================

def apply_move_date(user, params):
    """确认后执行：按天数偏移修改活动的开始/结束日期"""
    activity = _resolve_by_id(user, params.get('target_id'))
    days = int(params.get('days', 0))
    if days == 0:
        return {'reply': '天数不能为 0', 'changed': False}

    direction = '推迟' if days > 0 else '提前'
    old_start = activity.start_date
    old_end = activity.end_date
    delta = timedelta(days=abs(days))

    if activity.start_date:
        activity.start_date = activity.start_date + delta if days > 0 else activity.start_date - delta
    if activity.end_date:
        activity.end_date = activity.end_date + delta if days > 0 else activity.end_date - delta

    activity.save(update_fields=['start_date', 'end_date', 'updated_at'])
    log_activity(user, activity, 'edited',
                 f'{direction} {abs(days)} 天（{old_start} → {activity.start_date}）（通过 AI 对话）')

    return {
        'reply': f'已将「{activity.name}」{direction} {abs(days)} 天',
        'card': 'activity',
        'activity_ids': [activity.id],
        'card_data': _activity_card_data(activity),
        'changed': True,
    }


@agent_tool('activities.move_date', '推迟或提前活动日期',
            'target（活动名称关键词）+ days（正数=推迟天数，负数=提前天数）',
            apply_fn=apply_move_date)
def tool_move_date(user, params):
    activity = _resolve_single(user, params.get('target') or params.get('name'))
    days = params.get('days')
    if days is None:
        raise ToolError('请告诉我推迟或提前几天（正数推迟，负数提前）')
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ToolError('天数必须是整数')
    if days == 0:
        raise ToolError('天数不能为 0')

    direction = '推迟' if days > 0 else '提前'
    return {
        'reply': f'准备将「{activity.name}」{direction} {abs(days)} 天，确认吗？',
        'card': 'confirm',
        'activity_ids': [activity.id],
        'card_data': {
            'kind': 'move_date',
            'name': activity.name,
            'direction': direction,
            'days': abs(days),
            'current_start': activity.start_date.isoformat() if activity.start_date else '未设定',
            'current_end': activity.end_date.isoformat() if activity.end_date else '未设定',
            'detail_url': reverse('activities:activity_detail', args=[activity.id]),
        },
        'action': {
            'tool': 'activities.move_date',
            'params': {**params, 'target_id': activity.id, 'days': days},
        },
    }


# ==================== P2：批量修改状态 ====================

def apply_batch_status(user, params):
    """确认后执行：批量修改匹配活动的状态"""
    status = params.get('status')
    target_ids = params.get('target_ids', [])
    activities = Activity.objects.filter(id__in=target_ids, user=user)
    count = 0
    for a in activities:
        old_label = dict(Activity.STATUS_CHOICES).get(a.status, a.status)
        a.status = status
        a.save(update_fields=['status', 'updated_at'])
        log_activity(user, a, 'status_changed',
                     f'状态「{old_label}」→「{dict(Activity.STATUS_CHOICES).get(status, status)}」（通过 AI 对话批量操作）')
        count += 1
    return {
        'reply': f'已将 {count} 个活动的状态修改为「{dict(Activity.STATUS_CHOICES).get(status, status)}」',
        'changed': True,
    }


@agent_tool('activities.batch_status', '批量修改活动状态',
            'status（目标状态）+ keyword/tag（筛选条件，匹配到的活动全部修改）',
            apply_fn=apply_batch_status)
def tool_batch_status(user, params):
    status = params.get('status')
    if status not in STATUS_LABELS:
        raise ToolError('目标状态无效，可选：计划 / 进行中 / 已完成 / 已取消')

    qs = visible_qs(Activity, user)
    keyword = str(params.get('keyword') or '').strip()
    tag = str(params.get('tag') or '').strip()
    if keyword:
        qs = qs.filter(name__icontains=keyword)
    if tag:
        qs = qs.filter(tags__name=tag)
    if not keyword and not tag:
        raise ToolError('请提供筛选条件（keyword 或 tag），避免误操作')

    count = qs.count()
    if count == 0:
        return {'reply': '没有找到符合条件的活动'}

    return {
        'reply': f'找到 {count} 个匹配活动，准备全部改为「{STATUS_LABELS[status]}」，确认吗？',
        'card': 'confirm',
        'activity_ids': list(qs.values_list('id', flat=True)[:20]),
        'card_data': {
            'kind': 'batch_status',
            'count': count,
            'target_status': STATUS_LABELS[status],
            'condition': f'{"关键词: " + keyword if keyword else ""}{"标签: " + tag if tag else ""}',
        },
        'action': {
            'tool': 'activities.batch_status',
            'params': {'status': status, 'target_ids': list(qs.values_list('id', flat=True))},
        },
    }


# ==================== P2：设置预算 ====================

def _apply_set_budget(user, params):
    """set_budget 的确认执行函数"""
    target = params.get('target', '').strip()
    budget = Decimal(params.get('budget', '0'))
    activity = _resolve_single(user, target)
    activity.budget = budget
    activity.save(update_fields=['budget', 'updated_at'])
    log_activity(user, activity, 'edited', f'设置预算 ¥{budget}（通过 AI 对话）')
    return {
        'reply': f'已为「{activity.name}」设置预算 ¥{budget}',
        'card': 'activity',
        'card_data': _activity_card_data(activity),
        'activity_ids': [activity.id],
        'changed': True,
    }


@agent_tool('activities.set_budget', '为活动设置预算上限',
            'target（活动名称关键词）+ budget（预算金额，必填，正数）',
            apply_fn=_apply_set_budget)
def tool_set_budget(user, params):
    target = params.get('target', '').strip()
    if not target:
        raise ToolError('请告诉我要为哪个活动设置预算')

    budget = params.get('budget')
    if budget is None:
        raise ToolError('请告诉我预算金额')
    try:
        budget = Decimal(str(budget))
        if budget <= 0:
            raise ToolError('预算金额必须大于 0')
    except (InvalidOperation, ValueError, TypeError):
        raise ToolError('预算金额格式不正确')

    activity = _resolve_single(user, target)

    return {
        'reply': f'将为「{activity.name}」设置预算 ¥{budget}',
        'card': 'confirm',
        'card_data': {
            'kind': 'set_budget',
            'name': activity.name,
            'budget': str(budget),
            'detail_url': reverse('activities:activity_detail', args=[activity.id]),
        },
        'activity_ids': [activity.id],
        'action': {
            'tool': 'activities.set_budget',
            'params': {'target': target, 'budget': str(budget), 'target_id': activity.id},
        },
    }
