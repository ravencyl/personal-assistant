import json
import logging
import re
from datetime import date, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from taggit.models import Tag

from .forms import ActivityForm
from .models import Activity, Participant, ActivityLog, Expense
from .parsing import parse_quick_input
from core.utils import visible_qs, get_visible

logger = logging.getLogger(__name__)


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


def _snapshot(activity):
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


def _fmt(field, value):
    if value in (None, ''):
        return '空'
    if field == 'status':
        return dict(Activity.STATUS_CHOICES).get(value, str(value))
    return str(value)


def _diff_part(label, old_set, new_set):
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    parts = []
    if added:
        parts.append('+' + '、'.join(added))
    if removed:
        parts.append('-' + '、'.join(removed))
    return f'{label}{" ".join(parts)}' if parts else ''


def _edit_summary(old, activity):
    """对比编辑前后快照，生成变更摘要"""
    changes = []
    for field, label in _EDIT_FIELDS:
        new_v = getattr(activity, field)
        if old[field] != new_v:
            changes.append(f'{label} {_fmt(field, old[field])} → {_fmt(field, new_v)}')
    if old['parent'] != activity.parent:
        old_p = old['parent'].name if old['parent'] else '无'
        new_p = activity.parent.name if activity.parent else '无'
        changes.append(f'父活动 {old_p} → {new_p}')
    tag_diff = _diff_part('标签 ', old['tags'], set(activity.tags.names()))
    if tag_diff:
        changes.append(tag_diff)
    p_diff = _diff_part('参与者 ', old['participants'],
                        set(activity.participants.values_list('name', flat=True)))
    if p_diff:
        changes.append(p_diff)
    return '；'.join(changes)[:500]


def _user_tag_names(user):
    """可见范围内活动上使用过的全部标签名（供表单 autocomplete 建议）"""
    activity_ids = visible_qs(Activity, user).values('id')
    return list(Tag.objects.filter(
        taggit_taggeditem_items__content_type=ContentType.objects.get_for_model(Activity),
        taggit_taggeditem_items__object_id__in=activity_ids,
    ).distinct().values_list('name', flat=True).order_by('name'))


@login_required
@ensure_csrf_cookie
@require_POST
def parse_quick_input_view(request):
    """快速输入解析：AI 优先（Qoder general agent），失败/未配置时降级规则解析"""
    text = (request.POST.get('text') or '').strip()
    if not text:
        return JsonResponse({'error': '请输入内容'}, status=400)
    if len(text) > 500:
        return JsonResponse({'error': '输入过长，请控制在 500 字以内'}, status=400)

    today = timezone.localdate()
    data = _normalize(_ai_parse(text, today) or {}, today)
    source = 'ai' if data.get('name') else 'rule'
    if source == 'rule':
        data = _normalize(parse_quick_input(text, today), today)
    if not data.get('name'):
        return JsonResponse({
            'error': '未能识别出活动名称，请写得更具体些，例如「8月25到28日去上海出差 预算3000」',
        }, status=400)
    data['source'] = source
    return JsonResponse(data)


@login_required
@require_POST
def activity_quick_create(request):
    """列表页快速创建（快速输入预览卡片确认；一律创建为顶级活动）"""
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    data = _normalize(data, timezone.localdate())
    if not data.get('name'):
        return JsonResponse({'error': '活动名称不能为空'}, status=400)

    activity = Activity.objects.create(
        user=request.user,
        name=data['name'],
        start_date=data.get('start_date'),
        end_date=data.get('end_date'),
        status=data.get('status', 'planned'),
    )
    if data.get('cost') and float(data['cost']) > 0:
        Expense.objects.create(
            activity=activity,
            user=request.user,
            amount=data['cost'],
            category='other',
            note='快速输入创建',
        )
    if data.get('tags'):
        activity.tags.add(*data['tags'])
    if data.get('participants'):
        participants = [
            Participant.objects.get_or_create(user=request.user, name=name)[0]
            for name in data['participants']
        ]
        activity.participants.set(participants)
    log_activity(request.user, activity, 'created', '通过快速输入创建')

    return JsonResponse({
        'id': activity.id,
        'name': activity.name,
        'url': reverse('activities:activity_detail', args=[activity.id]),
    })


def _ai_parse(text, today):
    """调用 Qoder general agent 解析快速输入；未配置/超时/异常时返回 None（由调用方降级）"""
    if not settings.QODER_ACCESS_TOKEN:
        return None
    try:
        from agents.models import AgentConfig, EnvironmentConfig
        from agents.services import get_service

        agent = (AgentConfig.objects.filter(is_active=True, purpose='general').first()
                 or AgentConfig.objects.filter(is_active=True).first())
        env = (EnvironmentConfig.objects.filter(is_default=True).first()
               or EnvironmentConfig.objects.first())
        if not agent or not env:
            return None

        prompt = (
            f'从用户输入中提取活动记录的字段，只返回一个 JSON 对象（不要解释、不要 markdown 代码块）。今天是 {today.isoformat()}。\n'
            '字段：name（活动名称，字符串）、start_date、end_date（YYYY-MM-DD，相对日期如明天/月底/下周五请换算为绝对日期，未写年份用当年）、'
            'cost（数字，单位元）、status（planned/in_progress/done/cancelled 之一）、tags（字符串数组）、participants（字符串数组）。\n'
            f'无法识别的字段不要出现在 JSON 中。用户输入："""{text}"""'
        )
        service = get_service()
        session = service.create_session(agent.agent_id, env.env_id)
        service.send_message(session['id'], prompt)
        reply = service.wait_for_response(session['id'], timeout=20, poll_interval=1.0)
        return _extract_json(reply)
    except Exception as e:
        logger.warning(f'快速输入 AI 解析失败，将降级规则解析: {e}')
        return None


def _extract_json(reply):
    """从 AI 回复中提取首个 JSON 对象（兼容前后有说明文字/代码块的情况）"""
    m = re.search(r'\{.*\}', reply or '', re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _normalize(data, today):
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


@login_required
@ensure_csrf_cookie
def activity_list(request):
    """活动列表（默认树形结构可折叠，筛选/排序时为平铺列表）"""
    status_filter = request.GET.get('status', '')
    tag_filter = request.GET.get('tag', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    participant_filter = request.GET.get('participant', '').strip()
    keyword_filter = request.GET.get('keyword', '').strip()
    sort = request.GET.get('sort', '').strip()

    # 表头排序字段映射（key 为 URL 参数值，value 为模型/注解字段）
    sort_fields = {
        'name': 'name',
        'status': 'status',
        'start_date': 'start_date',
        'cost': 'cost',
        'sub_count': 'sub_count',
    }

    # 一次性聚合子活动数量，避免 N+1；预取标签（超级用户可见全部数据）
    all_activities = list(visible_qs(Activity, request.user).prefetch_related('tags').annotate(
        sub_count=Count('children', distinct=True),
    ))

    # 筛选条件用于计算命中集合（树形结构始终保留，命中节点及其祖先链可见）
    matched = filter_activities(request.user, {
        'status': status_filter,
        'tag': tag_filter,
        'date_from': date_from,
        'date_to': date_to,
        'participant': participant_filter,
        'keyword': keyword_filter,
    })

    has_filter = bool(status_filter or tag_filter or date_from or date_to
                      or participant_filter or keyword_filter)

    # 排序：校验字段合法性（排序作用于树内同级节点，不打乱层级）
    sort_key = sort.lstrip('-')
    if sort_key in sort_fields:
        desc = sort.startswith('-')
        sort_field = sort_fields[sort_key]
    else:
        sort = ''
        sort_field = None

    # 深度优先遍历构建活动树，为每行附加 depth 层级
    children_map = {}
    for a in all_activities:
        children_map.setdefault(a.parent_id, []).append(a)

    # 用内存中的 children_map 递归计算累计费用（自身 Expense + 所有后代 Expense）
    # 先批量查每个活动的直接费用合计
    from django.db.models import Sum
    activity_ids = [a.id for a in all_activities]
    expense_totals = dict(
        Expense.objects.filter(activity_id__in=activity_ids)
        .values_list('activity_id')
        .annotate(total=Sum('amount'))
        .values_list('activity_id', 'total')
    )

    cost_cache = {}

    def compute_cost(a):
        if a.id not in cost_cache:
            cost_cache[a.id] = float(expense_totals.get(a.id, 0) or 0) + sum(
                compute_cost(c) for c in children_map.get(a.id, [])
            )
        return cost_cache[a.id]

    for a in all_activities:
        a.show_cost = compute_cost(a)

    # 同级排序：空日期始终排最后，费用按累计值排序
    if sort_field:
        for siblings in children_map.values():
            if sort_field == 'start_date':
                with_val = [a for a in siblings if a.start_date is not None]
                nulls = [a for a in siblings if a.start_date is None]
                with_val.sort(key=lambda a: a.start_date, reverse=desc)
                siblings[:] = with_val + nulls
            elif sort_field == 'cost':
                siblings.sort(key=lambda a: a.show_cost or 0, reverse=desc)
            else:
                siblings.sort(key=lambda a: getattr(a, sort_field) or '', reverse=desc)

    rows = []

    def walk(parent_id, depth):
        for a in children_map.get(parent_id, []):
            a.depth = depth
            a.has_children = bool(children_map.get(a.id))
            rows.append(a)
            walk(a.id, depth + 1)

    walk(None, 0)

    # 筛选时保留命中活动及其祖先链（维持树形），并自动展开全部
    if has_filter:
        matched_ids = set(matched.values_list('id', flat=True))
        by_id = {a.id: a for a in all_activities}
        keep = set(matched_ids)
        for a_id in matched_ids:
            parent_id = by_id[a_id].parent_id
            while parent_id and parent_id not in keep:
                keep.add(parent_id)
                parent_id = by_id[parent_id].parent_id
        rows = [a for a in rows if a.id in keep]
        # 折叠箭头只在实际可见子节点存在时显示
        present_parents = {a.parent_id for a in rows if a.parent_id}
        for a in rows:
            a.has_children = a.id in present_parents
        expand_all = True
    else:
        expand_all = False

    # 快捷筛选高亮判断
    today = timezone.localdate()
    quick = ''
    if date_from and date_to:
        if (date_from, date_to) == (str(today - timedelta(days=6)), str(today)):
            quick = '7d'
        elif (date_from, date_to) == (str(today - timedelta(days=29)), str(today)):
            quick = '30d'

    # 表头排序链接需保留现有筛选参数（去掉 sort 本身）
    filter_params = request.GET.copy()
    filter_params.pop('sort', None)
    filter_qs = filter_params.urlencode()

    # 状态筛选需保留日期参数（不含 status/sort）
    date_params = {}
    if date_from:
        date_params['date_from'] = date_from
    if date_to:
        date_params['date_to'] = date_to
    date_qs = urlencode(date_params)

    # 筛选面板默认折叠；URL 带任一筛选/排序参数时自动展开
    filters_active = bool(status_filter or tag_filter or date_from or date_to or sort
                          or participant_filter or keyword_filter)
    active_filter_count = sum([
        bool(status_filter), bool(tag_filter), bool(date_from or date_to), bool(sort),
        bool(participant_filter or keyword_filter),
    ])

    # 标签筛选需保留状态/日期/排序参数（不含 tag）
    tag_link_params = {k: v for k, v in date_params.items()}
    if status_filter:
        tag_link_params['status'] = status_filter
    if sort:
        tag_link_params['sort'] = sort
    if participant_filter:
        tag_link_params['participant'] = participant_filter
    if keyword_filter:
        tag_link_params['keyword'] = keyword_filter
    tag_link_qs = urlencode(tag_link_params)

    # 首页问候头部：时段问候 + 日期星期 + 今日摘要
    hour = timezone.localtime().hour
    if hour < 6:
        greeting = '夜深了，早点休息'
    elif hour < 12:
        greeting = '早上好'
    elif hour < 14:
        greeting = '中午好'
    elif hour < 18:
        greeting = '下午好'
    else:
        greeting = '晚上好'
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    today_display = f'{today.month}月{today.day}日 · {weekdays[today.weekday()]}'
    ongoing_count = sum(1 for a in all_activities if a.status == 'in_progress')
    today_count = sum(
        1 for a in all_activities
        if (a.start_date and a.start_date <= today and (a.end_date is None or a.end_date >= today))
        or (a.start_date is None and a.end_date == today)
    )

    return render(request, 'activities/activity_list.html', {
        'activities': rows,
        'greeting': greeting,
        'today_display': today_display,
        'ongoing_count': ongoing_count,
        'today_count': today_count,
        'status_filter': status_filter,
        'status_choices': Activity.STATUS_CHOICES,
        'tag_filter': tag_filter,
        'all_tags': _user_tag_names(request.user),
        'tag_link_qs': tag_link_qs,
        'filters_active': filters_active,
        'active_filter_count': active_filter_count,
        'tree_mode': True,
        'expand_all': expand_all,
        'date_from': date_from,
        'date_to': date_to,
        'quick': quick,
        'quick_7d_from': str(today - timedelta(days=6)),
        'quick_30d_from': str(today - timedelta(days=29)),
        'today_str': str(today),
        'sort': sort,
        'filter_qs': filter_qs,
        'date_qs': date_qs,
    })


@login_required
def activity_detail(request, activity_id):
    """活动详情（含子任务时间轴、费用明细与操作日志）"""
    activity = get_visible(Activity, request.user, id=activity_id)

    # 子任务按时间轴排序：可用日期（开始优先，其次结束）从早到晚，无日期排最后
    children = list(activity.children.prefetch_related('tags', 'participants').all())
    children.sort(key=lambda c: ((c.start_date or c.end_date) is None,
                                 c.start_date or c.end_date or date.min))
    for child in children:
        d = child.start_date or child.end_date
        child.timeline_label = d.strftime('%m-%d') if d else '未设定'
        child.timeline_year = d.strftime('%Y') if d else ''
        # 子活动的费用合计（自身 Expense + 后代 Expense，用于时间轴展示）
        child.expenses_total = child.total_cost

    # 费用明细
    expenses = list(activity.expenses.all())

    return render(request, 'activities/activity_detail.html', {
        'activity': activity,
        'children': children,
        'participants': activity.participants.all(),
        'status_choices': Activity.STATUS_CHOICES,
        'logs': activity.logs.select_related('user')[:50],
        'expenses': expenses,
        'expense_categories': Expense.CATEGORY_CHOICES,
        'today_date': timezone.localdate().isoformat(),
    })


@login_required
@require_POST
def activity_set_status(request, activity_id):
    """快捷修改活动状态"""
    activity = get_visible(Activity, request.user, id=activity_id)
    status = request.POST.get('status', '')
    valid = dict(Activity.STATUS_CHOICES)
    if status not in valid:
        messages.error(request, '无效的状态值')
    elif status != activity.status:
        old_label = valid.get(activity.status, activity.status)
        activity.status = status
        activity.save(update_fields=['status', 'updated_at'])
        log_activity(request.user, activity, 'status_changed',
                     f'状态「{old_label}」→「{valid[status]}」')
        messages.success(request, f'状态已更新为「{valid[status]}」')
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('activities:activity_detail', activity.id)


@login_required
def activity_create(request):
    """新建活动"""
    if request.method == 'POST':
        form = ActivityForm(request.POST, user=request.user)
        if form.is_valid():
            activity = form.save(commit=False)
            activity.user = request.user
            activity.save()
            form.save_m2m()
            form.save_participants(activity)
            children = form.save_children(activity)
            log_activity(request.user, activity, 'created')
            for child in children:
                log_activity(request.user, child, 'created', f'随父活动「{activity.name}」一并创建')
            messages.success(request, f'活动「{activity.name}」已创建')
            return redirect('activities:activity_detail', activity.id)
    else:
        form = ActivityForm(user=request.user)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '新建活动',
        'all_participants': list(Participant.objects.filter(user=request.user).values_list('name', flat=True)),
        'all_tags': _user_tag_names(request.user),
    })


@login_required
def activity_edit(request, activity_id):
    """编辑活动（超级用户编辑他人活动时保持原属主）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    owner = activity.user

    if request.method == 'POST':
        old = _snapshot(activity)
        form = ActivityForm(request.POST, instance=activity, user=owner)
        if form.is_valid():
            form.save()
            form.save_participants(activity)
            log_activity(request.user, activity, 'edited', _edit_summary(old, activity))
            messages.success(request, f'活动「{activity.name}」已更新')
            return redirect('activities:activity_detail', activity.id)
    else:
        form = ActivityForm(instance=activity, user=owner)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '编辑活动',
        'activity': activity,
        'children': activity.children.all(),
        'all_participants': list(Participant.objects.filter(user=owner).values_list('name', flat=True)),
        'all_tags': _user_tag_names(owner),
    })


@login_required
@require_POST
def add_subactivity(request, activity_id):
    """快捷创建子活动（仅填名称；归属与父活动一致）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, '子活动名称不能为空')
    else:
        child = Activity.objects.create(
            user=activity.user,
            name=name,
            parent=activity,
            end_date=timezone.localdate(),
        )
        log_activity(request.user, activity, 'sub_created', f'创建子任务「{name}」')
        log_activity(request.user, child, 'created', f'在父活动「{activity.name}」下创建')
        messages.success(request, f'子活动「{name}」已创建')
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def activity_quick_sub(request, activity_id):
    """详情页快速创建子任务（解析后预览确认；归属当前活动）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    data = _normalize(data, timezone.localdate())
    if not data.get('name'):
        return JsonResponse({'error': '子任务名称不能为空'}, status=400)

    child = Activity.objects.create(
        user=activity.user,
        name=data['name'],
        parent=activity,
        start_date=data.get('start_date'),
        end_date=data.get('end_date'),
        status=data.get('status', 'planned'),
    )
    if data.get('cost') and float(data['cost']) > 0:
        Expense.objects.create(
            activity=activity,
            user=activity.user,
            amount=data['cost'],
            category='other',
            note=f'子任务「{child.name}」费用',
        )
    if data.get('tags'):
        child.tags.add(*data['tags'])
    if data.get('participants'):
        participants = [
            Participant.objects.get_or_create(user=activity.user, name=name)[0]
            for name in data['participants']
        ]
        child.participants.set(participants)
    log_activity(request.user, activity, 'sub_created', f'创建子任务「{child.name}」')
    log_activity(request.user, child, 'created', f'在父活动「{activity.name}」下创建')

    return JsonResponse({
        'id': child.id,
        'name': child.name,
        'url': reverse('activities:activity_detail', args=[child.id]),
    })


@login_required
@require_POST
def expense_create(request, activity_id):
    """为活动添加费用条目"""
    activity = get_visible(Activity, request.user, id=activity_id)
    try:
        amount = float(request.POST.get('amount', 0))
    except (TypeError, ValueError):
        return JsonResponse({'error': '金额格式不正确'}, status=400)
    if amount <= 0:
        return JsonResponse({'error': '金额必须大于 0'}, status=400)

    category = request.POST.get('category', 'other')
    if category not in dict(Expense.CATEGORY_CHOICES):
        category = 'other'

    paid_at = request.POST.get('paid_at', '').strip() or timezone.localdate().isoformat()
    if paid_at:
        try:
            date.fromisoformat(paid_at)
        except ValueError:
            paid_at = timezone.localdate().isoformat()

    note = request.POST.get('note', '').strip()[:255]

    expense = Expense.objects.create(
        activity=activity,
        user=request.user,
        amount=amount,
        category=category,
        paid_at=paid_at,
        note=note,
    )
    log_activity(request.user, activity, 'edited',
                 f'添加费用 ¥{amount} [{expense.get_category_display()}]{" " + note if note else ""}')

    if request.headers.get('HX-Request') or request.headers.get('Accept') == 'application/json':
        return JsonResponse({
            'id': expense.id,
            'amount': float(expense.amount),
            'category': expense.get_category_display(),
            'note': expense.note,
            'paid_at': expense.paid_at,
        })
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def expense_edit(request, expense_id):
    """编辑费用条目"""
    expense = get_visible(Expense, request.user, id=expense_id)
    activity = expense.activity
    try:
        amount = float(request.POST.get('amount', 0))
    except (TypeError, ValueError):
        return JsonResponse({'error': '金额格式不正确'}, status=400)
    if amount <= 0:
        return JsonResponse({'error': '金额必须大于 0'}, status=400)

    category = request.POST.get('category', 'other')
    if category not in dict(Expense.CATEGORY_CHOICES):
        category = 'other'

    paid_at = request.POST.get('paid_at', '').strip() or None
    if paid_at:
        try:
            date.fromisoformat(paid_at)
        except ValueError:
            paid_at = None

    note = request.POST.get('note', '').strip()[:255]

    expense.amount = amount
    expense.category = category
    expense.paid_at = paid_at
    expense.note = note
    expense.save()
    log_activity(request.user, activity, 'edited',
                 f'编辑费用 ¥{amount} [{expense.get_category_display()}]{" " + note if note else ""}')

    if request.headers.get('HX-Request') or request.headers.get('Accept') == 'application/json':
        return JsonResponse({
            'id': expense.id,
            'amount': float(expense.amount),
            'category': expense.get_category_display(),
            'note': expense.note,
            'paid_at': expense.paid_at,
        })
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def expense_delete(request, expense_id):
    """删除费用条目"""
    expense = get_visible(Expense, request.user, id=expense_id)
    activity = expense.activity
    note_desc = f'¥{expense.amount} [{expense.get_category_display()}]'
    if expense.note:
        note_desc += f' {expense.note}'
    expense.delete()
    log_activity(request.user, activity, 'edited', f'删除费用 {note_desc}')

    if request.headers.get('HX-Request') or request.headers.get('Accept') == 'application/json':
        return JsonResponse({'ok': True})
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def activity_delete(request, activity_id):
    """删除活动"""
    activity = get_visible(Activity, request.user, id=activity_id)
    name = activity.name
    log_activity(request.user, activity, 'deleted')
    activity.delete()
    messages.success(request, f'活动「{name}」已删除')
    return redirect('activities:activity_list')


@login_required
def activity_calendar(request):
    """活动日历视图（月历）"""
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        if not (1 <= month <= 12):
            raise ValueError
    except (ValueError, TypeError):
        year, month = today.year, today.month

    # 计算月历网格
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)

    # 日历从周一开始，填充前后空白
    start_offset = first_day.weekday()  # 0=Monday
    calendar_start = first_day - timedelta(days=start_offset)
    weeks = []
    current = calendar_start
    for _ in range(6):  # 最多6周
        week = []
        for _ in range(7):
            week.append({
                'date': current,
                'day': current.day,
                'in_month': current.month == month,
                'is_today': current == today,
                'date_str': current.isoformat(),
            })
            current += timedelta(days=1)
        weeks.append(week)
        if current > last_day and len(weeks) >= 5:
            break

    # 上月/下月导航
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return render(request, 'activities/activity_calendar.html', {
        'year': year,
        'month': month,
        'month_name': f'{year}年{month}月',
        'weeks': weeks,
        'first_day': first_day,
        'last_day': last_day,
        'prev_year': prev_year,
        'prev_month': prev_month,
        'next_year': next_year,
        'next_month': next_month,
        'today': today,
        'weekdays': ['一', '二', '三', '四', '五', '六', '日'],
    })


@login_required
def calendar_data(request):
    """日历数据 API：返回指定月份的活动（JSON）"""
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        if not (1 <= month <= 12):
            raise ValueError
    except (ValueError, TypeError):
        year, month = today.year, today.month

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    # 查询与本月有交集的活动（开始日期 <= 月末 且 结束日期 >= 月初）
    activities = visible_qs(Activity, request.user).filter(
        start_date__lte=month_end,
    ).filter(
        # 结束日期 >= 月初 或 无结束日期但开始日期 >= 月初
        models.Q(end_date__gte=month_start) | models.Q(end_date__isnull=True, start_date__gte=month_start)
    ).prefetch_related('tags')

    data = []
    for a in activities:
        # 计算在本月内的显示区间
        display_start = a.start_date or month_start
        display_end = a.end_date or a.start_date or month_end
        if display_start < month_start:
            display_start = month_start
        if display_end > month_end:
            display_end = month_end

        # 状态颜色
        color_map = {
            'planned': '#a1a1aa',  # zinc-400
            'in_progress': '#18181b',  # zinc-900
            'done': '#22c55e',  # green-500
            'cancelled': '#f87171',  # red-400
        }

        data.append({
            'id': a.id,
            'name': a.name,
            'start_date': display_start.isoformat(),
            'end_date': display_end.isoformat(),
            'status': a.status,
            'status_label': a.get_status_display(),
            'color': color_map.get(a.status, '#a1a1aa'),
            'url': reverse('activities:activity_detail', args=[a.id]),
            'tags': list(a.tags.names()),
        })

    return JsonResponse({'activities': data})


@login_required
def daily_view(request):
    """每日简报：展示当天活动概况、进行中/即将开始/近期完成的活动"""
    today = timezone.localdate()
    qs = visible_qs(Activity, request.user).prefetch_related('tags', 'participants')

    # ── 今日活动：start_date <= today 且 (end_date >= today 或无 end_date) ──
    ongoing = list(qs.filter(
        start_date__lte=today,
    ).filter(
        models.Q(end_date__gte=today) | models.Q(end_date__isnull=True, start_date=today)
    ).exclude(status='cancelled').exclude(status='done'))
    ongoing_ids = [a.id for a in ongoing]

    # ── 今日开始/结束（排除已在 ongoing 中的，避免重复） ──
    starting_today = list(qs.filter(start_date=today).exclude(
        status='cancelled'
    ).exclude(id__in=ongoing_ids))
    ending_today = list(qs.filter(end_date=today).exclude(
        status='cancelled'
    ).exclude(id__in=ongoing_ids).exclude(
        id__in=[a.id for a in starting_today]
    ))

    # ── 即将开始（未来 7 天） ──
    upcoming = qs.filter(
        start_date__gt=today,
        start_date__lte=today + timedelta(days=7),
    ).exclude(status='cancelled').order_by('start_date')[:10]

    # ── 近期完成（最近 3 天） ──
    recently_done = qs.filter(
        status='done',
        end_date__gte=today - timedelta(days=3),
    ).order_by('-end_date')[:10]

    # ── 进行中（全局） ──
    in_progress = qs.filter(status='in_progress').exclude(
        id__in=[a.id for a in ongoing]
    ).order_by('-start_date')[:10]

    # ── 统计：今日实际消费（按 paid_at 筛选） ──
    from django.db.models import Sum
    today_expense = Expense.objects.filter(
        user=request.user,
        paid_at=today,
    ).aggregate(s=Sum('amount'))['s'] or 0

    # 问候
    hour = timezone.localtime().hour
    if hour < 6:
        greeting = '夜深了，早点休息'
    elif hour < 12:
        greeting = '早上好'
    elif hour < 14:
        greeting = '中午好'
    elif hour < 18:
        greeting = '下午好'
    else:
        greeting = '晚上好'
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    today_display = f'{today.year}年{today.month}月{today.day}日 · {weekdays[today.weekday()]}'

    # 为每个活动附加费用合计（避免 N+1）
    def attach_costs(activities):
        ids = [a.id for a in activities]
        totals = dict(
            Expense.objects.filter(activity_id__in=ids)
            .values_list('activity_id').annotate(total=Sum('amount'))
            .values_list('activity_id', 'total')
        ) if ids else {}
        for a in activities:
            a.expense_total = float(totals.get(a.id, 0) or 0)
        return activities

    return render(request, 'activities/daily.html', {
        'today': today,
        'today_display': today_display,
        'greeting': greeting,
        'ongoing': attach_costs(list(ongoing)),
        'starting_today': attach_costs(list(starting_today)),
        'ending_today': list(ending_today),
        'upcoming': attach_costs(list(upcoming)),
        'recently_done': list(recently_done),
        'in_progress': attach_costs(list(in_progress)),
        'today_expense': float(today_expense),
        'ongoing_count': len(ongoing) + len(starting_today),
        'in_progress_count': qs.filter(status='in_progress').count(),
    })
