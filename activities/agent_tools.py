"""活动 Agent 工具集（P0：query / get / set_status / create）

注册到 core.agent_registry，由对话编排器按意图分发调用。
约定：权限一律经 visible_qs / get_visible；写操作强制 log_activity；
目标不明确时抛 ToolError 让用户澄清，绝不猜测。
"""
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from core.agent_registry import ToolError, agent_tool
from core.utils import visible_qs

from .models import Activity, Participant
from .views import _normalize, filter_activities, log_activity

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
        'cost': float(activity.cost or 0),
        'description': activity.description,
        'tags': list(activity.tags.names()),
        'participants': list(activity.participants.values_list('name', flat=True)),
        'children': [_child_summary(c) for c in activity.children.all()[:6]],
        'children_count': activity.children.count(),
        'detail_url': reverse('activities:activity_detail', args=[activity.id]),
        'edit_url': reverse('activities:activity_edit', args=[activity.id]),
    }


def _resolve_single(user, target):
    """按名称关键词定位唯一活动；0 条或多条都要求用户澄清"""
    target = str(target or '').strip()
    if not target:
        raise ToolError('请告诉我目标活动的名称')
    qs = visible_qs(Activity, user).filter(name__icontains=target)
    count = qs.count()
    if count == 0:
        raise ToolError(f'没有找到名称包含「{target}」的活动')
    if count > 1:
        names = '、'.join(qs.values_list('name', flat=True)[:5])
        raise ToolError(f'匹配到多个活动：{names}，请说得更具体些')
    return qs.first()


@agent_tool('activities.query', '按条件查询活动列表',
            'name（名称关键词）、status、tag、date_from、date_to（YYYY-MM-DD），均可选')
def tool_query(user, params):
    qs = filter_activities(user, params).order_by('-start_date', '-created_at')
    total = qs.count()

    link_params = {k: str(params[k]).strip() for k in ('status', 'tag', 'date_from', 'date_to')
                   if str(params.get(k) or '').strip()}
    list_qs = urlencode(link_params)

    if total == 0:
        return {'reply': '没有找到符合条件的活动。要不要告诉我名称和日期，我来创建一个？'}

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
            'cost': float(a.cost or 0),
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
            'name（必填）、start_date/end_date（YYYY-MM-DD）、cost（数字，元）、'
            'status、tags（字符串数组）、participants（字符串数组）、parent（父活动名称，可选）')
def tool_create(user, params):
    data = _normalize(params, timezone.localdate())
    if not data.get('name'):
        raise ToolError('未能识别出活动名称，请写得更具体些')

    parent = None
    parent_name = str(params.get('parent') or '').strip()
    if parent_name:
        parent = visible_qs(Activity, user).filter(name__icontains=parent_name).first()

    activity = Activity.objects.create(
        user=user,
        name=data['name'],
        start_date=data.get('start_date'),
        end_date=data.get('end_date'),
        status=data.get('status', 'planned'),
        cost=data.get('cost', 0),
        parent=parent,
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
