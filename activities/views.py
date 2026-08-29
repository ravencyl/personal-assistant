import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import models
from django.db.models import Count, Sum
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from taggit.models import Tag

from .forms import ActivityForm
from .models import Activity, Participant, ActivityLog, Expense, ActivityTemplate, RecurringActivity, Attachment
from .parsing import parse_quick_input
from .utils import (edit_summary, filter_activities, log_activity,
                    normalize_input, snapshot_activity, budget_status,
                    exclude_daily_bucket, get_daily_bucket)
from core.utils import visible_qs, get_visible

logger = logging.getLogger(__name__)


def auto_start_activities(user=None):
    """将 start_date 已到但状态仍为 planned 的活动自动改为 in_progress。

    仅在状态为 planned 时变更，不覆盖用户手动设置的其他状态。
    每次变更都会写入 ActivityLog（操作人为 system）。
    返回变更数量。
    """
    today = timezone.localdate()
    qs = Activity.objects.filter(
        status='planned',
        start_date__lte=today,
        start_date__isnull=False,
    )
    if user is not None:
        qs = qs.filter(user=user)

    count = 0
    for activity in qs:
        activity.status = 'in_progress'
        activity.save(update_fields=['status', 'updated_at'])
        try:
            from django.contrib.auth import get_user_model
            system_user = get_user_model().objects.filter(username='system').first()
            if system_user:
                ActivityLog.objects.create(
                    user=system_user,
                    activity=activity,
                    activity_name=activity.name,
                    action='status_changed',
                    summary=f'系统自动调整：计划→进行中（开始日期 {activity.start_date} 已到）',
                )
        except Exception as e:
            logger.warning(f'自动状态变更日志写入失败 [{activity.name}]: {e}')
        count += 1

    if count:
        logger.info(f'自动状态变更: {count} 个活动 planned → in_progress')
    return count


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
    data = normalize_input(_ai_parse(text, today) or {}, today)
    source = 'ai' if data.get('name') else 'rule'
    if source == 'rule':
        data = normalize_input(parse_quick_input(text, today), today)
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
    data = normalize_input(data, timezone.localdate())
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


@login_required
@ensure_csrf_cookie
def activity_list(request):
    """活动列表（默认树形结构可折叠，筛选/排序时为平铺列表）"""
    # 自动将 start_date 已到的 planned 活动改为 in_progress
    auto_start_activities(request.user)

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

    # 一次性聚合子活动数量和费用数量，避免 N+1；预取标签（超级用户可见全部数据）
    # 「日常开支」归属桶为系统常驻活动，不展示在活动列表中（费用统计仍包含）
    all_activities = list(exclude_daily_bucket(
        visible_qs(Activity, request.user)
    ).prefetch_related('tags').annotate(
        sub_count=Count('children', distinct=True),
        expense_count=Count('expenses', distinct=True),
    ))

    # 筛选条件用于计算命中集合（树形结构始终保留，命中节点及其祖先链可见）
    matched = exclude_daily_bucket(filter_activities(request.user, {
        'status': status_filter,
        'tag': tag_filter,
        'date_from': date_from,
        'date_to': date_to,
        'participant': participant_filter,
        'keyword': keyword_filter,
    }))

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

    # 默认排序：start_date 倒序（最新开始的活动排最前，空日期排最后）
    default_sort = not sort_field
    if default_sort:
        sort_field = 'start_date'
        desc = True

    # 深度优先遍历构建活动树，为每行附加 depth 层级
    children_map = {}
    for a in all_activities:
        children_map.setdefault(a.parent_id, []).append(a)

    # 用内存中的 children_map 递归计算累计费用（自身 Expense + 所有后代 Expense）
    # 先批量查每个活动的直接费用合计
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

    # 清除搜索链接：保留其他筛选参数（不含 keyword）
    search_clear_params = {k: v for k, v in tag_link_params.items() if k != 'keyword'}
    search_clear_qs = urlencode(search_clear_params)

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
        'keyword_filter': keyword_filter,
        'match_count': matched.count() if has_filter else 0,
        'search_clear_qs': search_clear_qs,
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


def _subactivity_timeline(activity):
    """子任务时间轴数据：按可用日期（开始优先，其次结束）从早到晚，无日期排最后

    详情页首次渲染与内联手动创建端点的局部刷新共用。
    """
    children = list(activity.children.prefetch_related('tags', 'participants').all())
    children.sort(key=lambda c: ((c.start_date or c.end_date) is None,
                                 c.start_date or c.end_date or date.min))
    for child in children:
        d = child.start_date or child.end_date
        child.timeline_label = d.strftime('%m-%d') if d else '未设定'
        child.timeline_year = d.strftime('%Y') if d else ''
        # 子活动的费用合计（自身 Expense + 后代 Expense，用于时间轴展示）
        child.expenses_total = child.total_cost
    return children


@login_required
def activity_detail(request, activity_id):
    """活动详情（含子任务时间轴、费用明细与操作日志）"""
    # 自动将 start_date 已到的 planned 活动改为 in_progress
    auto_start_activities(request.user)
    activity = get_visible(Activity, request.user, id=activity_id)

    children = _subactivity_timeline(activity)

    # 费用明细
    expenses = list(activity.expenses.all())

    # 附件
    attachments = list(activity.attachments.all())

    # 预算状态
    budget_ratio, budget_level, budget_label = budget_status(activity)

    # 跨模块关联推荐
    from core.cross_link import get_related_content
    related = get_related_content(request.user, Activity, activity, limit=5)

    return render(request, 'activities/activity_detail.html', {
        'activity': activity,
        'children': children,
        'participants': activity.participants.all(),
        'status_choices': Activity.STATUS_CHOICES,
        'logs': activity.logs.select_related('user')[:50],
        'expenses': expenses,
        'expense_categories': Expense.CATEGORY_CHOICES,
        'today_date': timezone.localdate().isoformat(),
        'attachments': attachments,
        'budget_ratio': budget_ratio,
        'budget_level': budget_level,
        'budget_label': budget_label,
        'related_articles': related.get('articles', []),
        'related_notes': related.get('notes', []),
        # 手动内联创建子任务表单的 autocomplete 建议
        'tag_suggestions': _user_tag_names(request.user),
        'participant_suggestions': list(Participant.objects.filter(
            user=activity.user).values_list('name', flat=True).order_by('name')),
    })


@login_required
@require_POST
def activity_set_status(request, activity_id):
    """快捷修改活动状态"""
    activity = get_visible(Activity, request.user, id=activity_id)
    status = request.POST.get('status', '')
    valid = dict(Activity.STATUS_CHOICES)
    is_fragment = bool(request.headers.get('HX-Request'))
    if status not in valid:
        if is_fragment:
            return HttpResponse('错误：无效的状态值', status=400)
        messages.error(request, '无效的状态值')
    else:
        if status != activity.status:
            old_label = valid.get(activity.status, activity.status)
            activity.status = status
            activity.save(update_fields=['status', 'updated_at'])
            log_activity(request.user, activity, 'status_changed',
                         f'状态「{old_label}」→「{valid[status]}」')
            if not is_fragment:
                messages.success(request, f'状态已更新为「{valid[status]}」')
        if is_fragment:
            attach_costs([activity])
            return render(request, 'activities/_daily_card.html', {'activity': activity})
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
        old = snapshot_activity(activity)
        form = ActivityForm(request.POST, instance=activity, user=owner)
        if form.is_valid():
            form.save()
            form.save_participants(activity)
            log_activity(request.user, activity, 'edited', edit_summary(old, activity))
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
    data = normalize_input(data, timezone.localdate())
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


def _split_name_input(value):
    """内联表单的标签/参与者输入 → 去重列表（兼容逗号/顿号分隔与 # @ 前缀）"""
    if isinstance(value, str):
        items = re.split(r'[,，、]', value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    names, seen = [], set()
    for item in items:
        name = str(item).strip().lstrip('#@').strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name[:100])
    return names[:10]


def _parse_date_input(value):
    """内联表单日期输入 → date；空值返回 None，非法值抛 ValueError"""
    text = str(value or '').strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


@login_required
@require_POST
def subactivity_manual_create(request, activity_id):
    """详情页内联手动表单创建子任务（JSON，支持日期/状态/费用/标签/参与者）

    与 AI 快速入口 activity_quick_sub 的区别：字段由用户显式填写，校验失败返回
    400 + 友好文案（不静默丢弃），费用记在子任务自己名下便于时间轴直接展示。
    """
    activity = get_visible(Activity, request.user, id=activity_id)
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': '请求数据格式错误'}, status=400)

    name = str(data.get('name') or '').strip()
    if not name:
        return JsonResponse({'error': '子任务名称不能为空'}, status=400)

    try:
        start_date = _parse_date_input(data.get('start_date'))
        end_date = _parse_date_input(data.get('end_date'))
    except ValueError:
        return JsonResponse({'error': '日期格式不正确，请重新选择'}, status=400)
    if start_date and end_date and end_date < start_date:
        return JsonResponse({'error': '结束日期不能早于开始日期'}, status=400)

    status = str(data.get('status') or 'planned')
    if status not in dict(Activity.STATUS_CHOICES):
        status = 'planned'

    amount = None
    raw_amount = data.get('amount')
    if raw_amount not in (None, ''):
        try:
            # 用字符串构造 Decimal，避免 float 二进制的0000000001尾差写进库
            amount = Decimal(str(raw_amount).strip())
        except InvalidOperation:
            return JsonResponse({'error': '费用金额格式不正确'}, status=400)
        if amount < 0:
            return JsonResponse({'error': '费用金额不能为负数'}, status=400)

    child = Activity.objects.create(
        user=activity.user,          # 归属继承父活动
        name=name[:255],
        parent=activity,
        start_date=start_date,
        end_date=end_date,
        status=status,
    )
    if amount:
        Expense.objects.create(
            activity=child,
            user=activity.user,
            amount=amount,
            category='other',
            paid_at=timezone.localdate(),
            note=f'子任务「{child.name}」费用',
        )
    tags = _split_name_input(data.get('tags'))
    if tags:
        child.tags.add(*tags)
    participant_names = _split_name_input(data.get('participants'))
    if participant_names:
        child.participants.set([
            Participant.objects.get_or_create(user=activity.user, name=n)[0]
            for n in participant_names
        ])

    log_activity(request.user, activity, 'sub_created', f'创建子任务「{child.name}」')
    log_activity(request.user, child, 'created', f'在父活动「{activity.name}」下创建')

    # 返回重渲染后的子任务列表片段，供前端局部刷新（避开整页重载闪烁）
    children = _subactivity_timeline(activity)
    return JsonResponse({
        'id': child.id,
        'name': child.name,
        'url': reverse('activities:activity_detail', args=[child.id]),
        'children_count': len(children),
        'children_html': render_to_string('activities/_subactivity_items.html', {
            'activity': activity,
            'children': children,
        }, request=request),
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
    if amount < 0:
        return JsonResponse({'error': '金额不能为负数'}, status=400)

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
        agg = activity.expenses.aggregate(total=Sum('amount'), cnt=Count('id'))
        return JsonResponse({
            'id': expense.id,
            'amount': float(expense.amount),
            'category': expense.get_category_display(),
            'note': expense.note,
            'paid_at': expense.paid_at,
            'expense_total': float(agg['total'] or 0),
            'expense_count': agg['cnt'] or 0,
        })
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def expense_quick_create(request):
    """全局快记：一键记一笔费用（JSON 端点，由原生 fetch 消费）

    活动 id 可选；缺省记入「日常开支」归属桶。校验逻辑与 expense_create 一致。
    """
    try:
        amount = float(request.POST.get('amount', ''))
    except (TypeError, ValueError):
        return JsonResponse({'error': '请输入正确的金额'}, status=400)
    if amount < 0:
        return JsonResponse({'error': '金额不能为负数'}, status=400)

    activity_id = (request.POST.get('activity_id') or '').strip()
    if activity_id:
        activity = visible_qs(Activity, request.user).filter(id=activity_id).first()
        if not activity:
            return JsonResponse({'error': '活动不存在或无权访问'}, status=400)
    else:
        activity = get_daily_bucket(request.user)

    category = request.POST.get('category', 'other')
    if category not in dict(Expense.CATEGORY_CHOICES):
        category = 'other'

    paid_at = request.POST.get('paid_at', '').strip() or timezone.localdate().isoformat()
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
                 f'快记费用 ¥{amount} [{expense.get_category_display()}]{" " + note if note else ""}')

    agg = activity.expenses.aggregate(total=Sum('amount'), cnt=Count('id'))
    return JsonResponse({
        'success': True,
        'amount': float(expense.amount),
        'category': expense.get_category_display(),
        'activity_name': activity.name,
        'expense_total': float(agg['total'] or 0),
        'expense_count': agg['cnt'] or 0,
    })


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
    if amount < 0:
        return JsonResponse({'error': '金额不能为负数'}, status=400)

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
    """活动日历视图（月/周/日）"""
    today = timezone.localdate()
    mode = request.GET.get('mode', 'month')
    if mode not in ('month', 'week', 'day'):
        mode = 'month'

    ref_date_str = request.GET.get('date')
    if ref_date_str:
        try:
            ref_date = date.fromisoformat(ref_date_str)
        except (ValueError, TypeError):
            ref_date = today
    else:
        ref_date = today

    if mode == 'month':
        year = ref_date.year
        month = ref_date.month
        first_day = date(year, month, 1)
        last_day = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
        start_offset = first_day.weekday()
        calendar_start = first_day - timedelta(days=start_offset)
        weeks = []
        current = calendar_start
        for _ in range(6):
            week = []
            for _ in range(7):
                week.append({
                    'day': current.day,
                    'in_month': current.month == month,
                    'is_today': current == today,
                    'date_str': current.isoformat(),
                })
                current += timedelta(days=1)
            weeks.append(week)
            if current > last_day and len(weeks) >= 5:
                break
        if month == 1:
            prev_date = date(year - 1, 12, 1)
        else:
            prev_date = date(year, month - 1, 1)
        if month == 12:
            next_date = date(year + 1, 1, 1)
        else:
            next_date = date(year, month + 1, 1)
        title = f'{year}年{month}月'
        ctx = {'weeks': weeks, 'year': year, 'month': month}

    elif mode == 'week':
        monday = ref_date - timedelta(days=ref_date.weekday())
        sunday = monday + timedelta(days=6)
        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            days.append({
                'day': d.day,
                'weekday': ['一', '二', '三', '四', '五', '六', '日'][i],
                'is_today': d == today,
                'date_str': d.isoformat(),
            })
        prev_date = monday - timedelta(days=7)
        next_date = monday + timedelta(days=7)
        title = f'{monday.month}月{monday.day}日 – {sunday.month}月{sunday.day}日'
        ctx = {'days': days, 'week_start': monday.isoformat()}

    else:  # day
        d = ref_date
        hours = []
        for h in range(24):
            hours.append({'hour': h, 'label': f'{h:02d}:00'})
        prev_date = d - timedelta(days=1)
        next_date = d + timedelta(days=1)
        weekdays_cn = ['一', '二', '三', '四', '五', '六', '日']
        title = f'{d.month}月{d.day}日 周{weekdays_cn[d.weekday()]}'
        ctx = {'hours': hours, 'day_date': d.isoformat(), 'today_iso': today.isoformat()}

    prev_params = {'mode': mode, 'date': prev_date.isoformat()}
    next_params = {'mode': mode, 'date': next_date.isoformat()}
    today_params = {'mode': mode, 'date': today.isoformat()}

    return render(request, 'activities/activity_calendar.html', {
        **ctx,
        'mode': mode,
        'title': title,
        'weekdays': ['一', '二', '三', '四', '五', '六', '日'],
        'prev_params': prev_params,
        'next_params': next_params,
        'today_params': today_params,
        'today': today,
    })


@login_required
def calendar_data(request):
    """日历数据 API：返回指定区间的活动（JSON），支持月/周/日"""
    today = timezone.localdate()
    mode = request.GET.get('mode', 'month')

    if mode == 'week':
        ref = request.GET.get('date') or request.GET.get('week_start')
        try:
            monday = date.fromisoformat(ref)
        except (ValueError, TypeError):
            monday = today - timedelta(days=today.weekday())
        range_start = monday
        range_end = monday + timedelta(days=6)
    elif mode == 'day':
        try:
            d = date.fromisoformat(request.GET.get('date', today.isoformat()))
        except (ValueError, TypeError):
            d = today
        range_start = d
        range_end = d
    else:  # month
        try:
            year = int(request.GET.get('year', today.year))
            month = int(request.GET.get('month', today.month))
            if not (1 <= month <= 12):
                raise ValueError
        except (ValueError, TypeError):
            year, month = today.year, today.month
        range_start = date(year, month, 1)
        range_end = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)

    activities = visible_qs(Activity, request.user).filter(
        start_date__lte=range_end,
    ).filter(
        models.Q(end_date__gte=range_start) | models.Q(end_date__isnull=True, start_date__gte=range_start)
    ).prefetch_related('tags')

    color_map = {
        'planned': '#a1a1aa',
        'in_progress': '#18181b',
        'done': '#d4d4d8',
        'cancelled': '#e4e4e7',
    }

    data = []
    for a in activities:
        data.append({
            'id': a.id,
            'name': a.name,
            'start_date': a.start_date.isoformat(),
            'end_date': (a.end_date or a.start_date).isoformat(),
            'status': a.status,
            'status_label': a.get_status_display(),
            'color': color_map.get(a.status, '#a1a1aa'),
            'url': reverse('activities:activity_detail', args=[a.id]),
            'tags': list(a.tags.names()),
        })

    return JsonResponse({'activities': data})


def attach_costs(activities):
    """为活动列表附加费用合计/笔数/预算标注（避免 N+1）。"""
    ids = [a.id for a in activities]
    if ids:
        totals = dict(
            Expense.objects.filter(activity_id__in=ids)
            .values_list('activity_id').annotate(total=Sum('amount'))
            .values_list('activity_id', 'total')
        )
        counts = dict(
            Expense.objects.filter(activity_id__in=ids)
            .values_list('activity_id').annotate(cnt=Count('id'))
            .values_list('activity_id', 'cnt')
        )
    else:
        totals = {}
        counts = {}
    for a in activities:
        a.expense_total = float(totals.get(a.id, 0) or 0)
        a.expense_count = counts.get(a.id, 0)
        if a.budget:
            ratio = a.expense_total / float(a.budget) if float(a.budget) > 0 else 0
            a.budget_over = ratio >= 1.0
            a.budget_warning = ratio >= 0.8 and not a.budget_over
        else:
            a.budget_over = False
            a.budget_warning = False
    return activities


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

    # ── 进行中（全局，排除「日常开支」归属桶） ──
    in_progress = exclude_daily_bucket(qs.filter(status='in_progress')).exclude(
        id__in=[a.id for a in ongoing]
    ).order_by('-start_date')[:10]

    # ── 统计：今日实际消费（按 paid_at 筛选） ──
    today_expense = Expense.objects.filter(
        user=request.user,
        paid_at=today,
    ).aggregate(s=Sum('amount'))['s'] or 0

    # ── 本周消费合计 ──
    week_start = today - timedelta(days=today.weekday())
    this_week_expense = Expense.objects.filter(
        user=request.user,
        paid_at__gte=week_start,
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

    # AI 建议
    from core.suggestions import generate_suggestions
    suggestions = generate_suggestions(request.user)

    # 提醒：先触发到期提醒，再查询今日已触发但未处理的
    from core.models import Reminder, check_due_reminders
    check_due_reminders(request.user)
    pending_reminders = Reminder.objects.filter(
        user=request.user,
        status='fired',
        trigger_at__date=timezone.localdate(),
    ).order_by('trigger_at')[:10]

    # 打卡与提醒（习惯/子任务/提醒）：一次调用注入，早间（<18 点）展示，与晚间摘要按时段互斥
    from core.suggestions import generate_daily_plan
    today_plan = generate_daily_plan(request.user)

    # 循环活动今日实例
    today_instances = Activity.objects.filter(
        user=request.user,
        recurring_source__isnull=False,
        start_date=today,
        recurring_source__is_active=True,
    ).select_related('recurring_source')

    # 每日摘要（cron 预生成，只读库一次）
    from core.models import DailySummary
    daily_summary = DailySummary.objects.filter(
        user=request.user,
        summary_date=today,
    ).exclude(status='pending').first()

    return render(request, 'activities/daily.html', {
        'today': today,
        'today_display': today_display,
        'greeting': greeting,
        'ongoing': attach_costs(list(ongoing)),
        'starting_today': attach_costs(list(starting_today)),
        'ending_today': attach_costs(list(ending_today)),
        'upcoming': attach_costs(list(upcoming)),
        'recently_done': attach_costs(list(recently_done)),
        'in_progress': attach_costs(list(in_progress)),
        'today_expense': float(today_expense),
        'this_week_expense': float(this_week_expense),
        'ongoing_count': len(ongoing) + len(starting_today),
        'in_progress_count': exclude_daily_bucket(qs).filter(status='in_progress').count(),
        'suggestions': suggestions,
        'today_instances': today_instances,
        'pending_reminders': pending_reminders,
        'daily_summary': daily_summary,
        'today_plan': today_plan,
        'show_today_plan': hour < 18,
    })


@login_required
def template_list(request):
    """模板列表页面"""
    templates = ActivityTemplate.objects.filter(user=request.user)
    return render(request, 'activities/template_list.html', {
        'templates': templates,
    })


@login_required
@require_POST
def template_create(request):
    """创建新模板（JSON 请求）"""
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'error': '模板名称不能为空'}, status=400)
    
    description = (data.get('description') or '').strip()
    default_children = data.get('default_children', [])
    default_tags = data.get('default_tags', [])
    
    # 验证子任务格式
    if not isinstance(default_children, list):
        return JsonResponse({'error': '子任务格式错误'}, status=400)
    
    template = ActivityTemplate.objects.create(
        user=request.user,
        name=name,
        description=description,
        default_children=default_children,
        default_tags=default_tags,
    )
    
    return JsonResponse({
        'id': template.id,
        'name': template.name,
        'description': template.description,
        'default_children': template.default_children,
        'default_tags': template.default_tags,
    })


@login_required
@require_POST
def template_delete(request, template_id):
    """删除模板"""
    template = ActivityTemplate.objects.filter(
        id=template_id,
        user=request.user
    ).first()
    if not template:
        return JsonResponse({'error': '模板不存在'}, status=404)
    
    template.delete()
    return JsonResponse({'ok': True})


@login_required
@require_POST
def activity_from_template(request, template_id):
    """从模板创建活动（含预设子任务 + 标签）"""
    template = ActivityTemplate.objects.filter(
        id=template_id,
        user=request.user
    ).first()
    if not template:
        return JsonResponse({'error': '模板不存在'}, status=404)
    
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    
    # 活动名称：用户输入优先，否则用模板名
    name = (data.get('name') or template.name).strip()
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    
    # 创建主活动
    activity = Activity.objects.create(
        user=request.user,
        name=name,
        description=template.description,
        start_date=start_date,
        end_date=end_date,
        status='planned',
    )
    
    # 添加预设标签
    if template.default_tags:
        activity.tags.add(*template.default_tags)
    
    # 创建预设子任务
    children = []
    for child_data in template.default_children:
        child_name = (child_data.get('name') or '').strip()
        if child_name:
            child = Activity.objects.create(
                user=request.user,
                name=child_name,
                parent=activity,
                status='planned',
            )
            children.append(child)
            log_activity(request.user, child, 'created', f'从模板「{template.name}」创建')
    
    log_activity(request.user, activity, 'created', f'从模板「{template.name}」创建')
    
    return JsonResponse({
        'id': activity.id,
        'name': activity.name,
        'url': reverse('activities:activity_detail', args=[activity.id]),
        'children_count': len(children),
    })


@login_required
def expense_chart_data(request):
    """费用图表数据 API"""
    from django.db.models.functions import TruncMonth

    today = timezone.localdate()
    range_type = request.GET.get('range', 'month')  # month / week / category

    qs = Expense.objects.filter(user=request.user)

    if range_type == 'month':
        # 近 6 个月月度趋势
        six_months_ago = today - timedelta(days=180)
        data = list(
            qs.filter(paid_at__gte=six_months_ago)
            .annotate(month=TruncMonth('paid_at'))
            .values('month')
            .annotate(total=Sum('amount'))
            .order_by('month')
        )
        return JsonResponse({
            'labels': [d['month'].strftime('%Y-%m') for d in data],
            'values': [float(d['total']) for d in data],
        })

    elif range_type == 'week':
        # 本周 vs 上周每日对比
        this_week_start = today - timedelta(days=today.weekday())
        last_week_start = this_week_start - timedelta(days=7)

        this_week = qs.filter(paid_at__gte=this_week_start, paid_at__lte=today)
        last_week = qs.filter(paid_at__gte=last_week_start, paid_at__lt=this_week_start)

        weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        this_data = [0.0] * 7
        last_data = [0.0] * 7

        for e in this_week:
            if e.paid_at:
                idx = (e.paid_at - this_week_start).days
                if 0 <= idx < 7:
                    this_data[idx] += float(e.amount)
        for e in last_week:
            if e.paid_at:
                idx = (e.paid_at - last_week_start).days
                if 0 <= idx < 7:
                    last_data[idx] += float(e.amount)

        return JsonResponse({
            'labels': weekdays,
            'this_week': this_data,
            'last_week': last_data,
        })

    elif range_type == 'category':
        # 分类饼图（近 12 个月）
        year_ago = today - timedelta(days=365)
        data = list(
            qs.filter(paid_at__gte=year_ago)
            .values('category')
            .annotate(total=Sum('amount'))
            .order_by('-total')
        )
        category_labels = dict(Expense.CATEGORY_CHOICES)
        return JsonResponse({
            'labels': [category_labels.get(d['category'], d['category']) for d in data],
            'values': [float(d['total']) for d in data],
        })

    elif range_type == 'month_category':
        # 单月分类明细（month 参数 YYYY-MM，缺省/非法回退当月）
        m = re.match(r'^(\d{4})-(\d{1,2})$', (request.GET.get('month') or '').strip())
        if m and 1 <= int(m.group(2)) <= 12:
            year, month = int(m.group(1)), int(m.group(2))
        else:
            year, month = today.year, today.month
        data = list(
            qs.filter(paid_at__year=year, paid_at__month=month)
            .values('category')
            .annotate(total=Sum('amount'))
            .order_by('-total')
        )
        category_labels = dict(Expense.CATEGORY_CHOICES)
        grand_total = sum(float(d['total']) for d in data)
        items = [{
            'category': d['category'],
            'label': category_labels.get(d['category'], d['category']),
            'amount': float(d['total']),
            'pct': round(float(d['total']) * 100 / grand_total, 1) if grand_total else 0,
        } for d in data]
        return JsonResponse({
            'month': f'{year:04d}-{month:02d}',
            'total': grand_total,
            'items': items,
        })

    return JsonResponse({'labels': [], 'values': []})


@login_required
@require_POST
def attachment_upload(request, activity_id):
    """上传附件"""
    activity = get_visible(Activity, request.user, id=activity_id)
    uploaded_file = request.FILES.get('file')
    if not uploaded_file:
        return JsonResponse({'error': '请选择文件'}, status=400)

    # 限制文件大小 10MB
    if uploaded_file.size > 10 * 1024 * 1024:
        return JsonResponse({'error': '文件大小不能超过 10MB'}, status=400)

    attachment = Attachment.objects.create(
        activity=activity,
        user=request.user,
        file=uploaded_file,
        filename=uploaded_file.name,
        content_type=uploaded_file.content_type or '',
        size=uploaded_file.size,
    )
    log_activity(request.user, activity, 'edited', f'上传附件「{attachment.filename}」')

    if request.headers.get('HX-Request'):
        return render(request, 'activities/_attachment_item.html', {
            'attachment': attachment,
        })

    return JsonResponse({
        'id': attachment.id,
        'filename': attachment.filename,
        'size': attachment.size_display,
        'is_image': attachment.is_image,
        'url': attachment.file.url,
    })


@login_required
@require_POST
def attachment_delete(request, attachment_id):
    """删除附件"""
    attachment = get_object_or_404(Attachment, id=attachment_id, user=request.user)
    activity = attachment.activity
    filename = attachment.filename
    attachment.file.delete()  # 删除物理文件
    attachment.delete()
    log_activity(request.user, activity, 'edited', f'删除附件「{filename}」')

    if request.headers.get('HX-Request'):
        return JsonResponse({'ok': True})

    return redirect('activities:activity_detail', activity.id)


@login_required
def expense_category_suggest(request, activity_id):
    """基于用户历史费用数据推荐类别排序（JSON）"""
    activity = get_visible(Activity, request.user, id=activity_id)

    from django.core.cache import cache
    cache_key = f'expense_cat_dist_{request.user.id}'
    cat_dist = cache.get(cache_key)
    if cat_dist is None:
        cat_dist = list(
            Expense.objects.filter(user=request.user)
            .values('category').annotate(n=Count('id'))
            .order_by('-n')
        )
        cache.set(cache_key, cat_dist, timeout=86400)

    # 按历史频率排序的类别列表；无历史数据时用默认顺序
    ordered = [c['category'] for c in cat_dist]
    default = [c[0] for c in Expense.CATEGORY_CHOICES]
    # 合并：历史有的排前面，没有的补后面
    seen = set(ordered)
    for c in default:
        if c not in seen:
            ordered.append(c)

    return JsonResponse({'categories': ordered})


@login_required
def expense_report(request):
    """费用报告页面"""
    today = timezone.localdate()

    # 本月费用合计
    month_start = today.replace(day=1)
    this_month_total = Expense.objects.filter(
        user=request.user, paid_at__gte=month_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    # 上月费用合计
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    last_month_total = Expense.objects.filter(
        user=request.user, paid_at__gte=last_month_start, paid_at__lt=month_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    # 本周费用合计
    week_start = today - timedelta(days=today.weekday())
    this_week_total = Expense.objects.filter(
        user=request.user, paid_at__gte=week_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    this_month_f = float(this_month_total)
    last_month_f = float(last_month_total)

    # 时间花费：全部活动耗时总和，人性化格式（批次4C，函数内局部导入避免触碰顶部 import 区）
    from .utils import fmt_duration
    total_duration_display = fmt_duration(Activity.objects.filter(user=request.user).aggregate(s=Sum('duration_minutes'))['s'])

    return render(request, 'activities/expense_report.html', {
        'this_month_total': this_month_f,
        'last_month_total': float(last_month_total),
        'this_week_total': float(this_week_total),
        'total_duration_display': total_duration_display,
        'month_change': (
            round((this_month_f - last_month_f) / last_month_f * 100, 1)
            if last_month_f > 0 else None
        ),
    })


@login_required
def recurring_list(request):
    """循环活动列表"""
    recurring = RecurringActivity.objects.filter(user=request.user)
    # 获取今天及未来的循环实例
    today = timezone.localdate()
    instances = Activity.objects.filter(
        user=request.user,
        recurring_source__isnull=False,
        start_date__gte=today,
    ).order_by('start_date')[:30]
    
    return render(request, 'activities/recurring_list.html', {
        'recurring': recurring,
        'instances': instances,
    })


@login_required
@require_POST
def recurring_create(request):
    """创建循环活动"""
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, '习惯名称不能为空')
        return redirect('activities:recurring_list')
    
    frequency = request.POST.get('frequency', 'daily')
    day_of_week = request.POST.get('day_of_week')
    day_of_month = request.POST.get('day_of_month')
    
    recurring = RecurringActivity.objects.create(
        user=request.user,
        name=name,
        frequency=frequency,
        day_of_week=int(day_of_week) if day_of_week else None,
        day_of_month=int(day_of_month) if day_of_month else None,
    )
    messages.success(request, f'循环活动「{name}」已创建')
    return redirect('activities:recurring_list')


@login_required
@require_POST
def recurring_delete(request, pk):
    """删除循环活动"""
    recurring = get_object_or_404(RecurringActivity, pk=pk, user=request.user)
    name = recurring.name
    recurring.delete()
    messages.success(request, f'循环活动「{name}」已删除')
    return redirect('activities:recurring_list')


@login_required
@require_POST
def recurring_toggle(request, pk):
    """切换启用/暂停"""
    recurring = get_object_or_404(RecurringActivity, pk=pk, user=request.user)
    recurring.is_active = not recurring.is_active
    recurring.save(update_fields=['is_active', 'updated_at'])
    status_text = '启用' if recurring.is_active else '暂停'
    messages.success(request, f'「{recurring.name}」已{status_text}')
    return redirect('activities:recurring_list')


@login_required
@require_POST
def recurring_checkin(request, activity_id):
    """打卡：将循环活动实例标记为 done"""
    activity = get_visible(Activity, request.user, id=activity_id)
    if activity.status != 'done':
        activity.status = 'done'
        activity.save(update_fields=['status', 'updated_at'])
        log_activity(request.user, activity, 'status_changed', '打卡完成')
        cache.delete(f'habit_heatmap_{request.user.id}')  # 失效打卡热力图缓存
    
    if request.headers.get('HX-Request'):
        return HttpResponse('<span class="text-sm text-zinc-400">✓ 已打卡</span>')
    return redirect(request.META.get('HTTP_REFERER', '/'))


@login_required
def habit_heatmap_data(request):
    """打卡热力图数据 API：近 365 天循环活动实例打卡（done）按日聚合（JSON）

    返回 {'heatmap': {'YYYY-MM-DD': count, ...}, 'total_days': int}，
    缓存 1 小时，打卡成功时失效。
    """
    cache_key = f'habit_heatmap_{request.user.id}'
    result = cache.get(cache_key)
    if result is None:
        today = timezone.localdate()
        start = today - timedelta(days=364)
        rows = list(
            visible_qs(Activity, request.user)
            .filter(recurring_source__isnull=False, status='done',
                    start_date__gte=start, start_date__lte=today)
            .values('start_date')
            .annotate(n=Count('id'))
        )
        result = {
            'heatmap': {row['start_date'].isoformat(): row['n'] for row in rows},
            'total_days': len(rows),
        }
        cache.set(cache_key, result, timeout=3600)
    return JsonResponse(result)


@login_required
def next_actions(request):
    """下一步行动：两组待办视图（只读，复用 Activity 自引用父子结构）

    组 1「待处理的子任务」：进行中的活动 + 其未完成（planned/in_progress）子活动；
    组 2「临近的计划活动」：未来 7 天内开始的顶层 planned 活动，按日期排序。
    「日常开支」等系统归属桶统一排除。
    """
    today = timezone.localdate()
    base_qs = visible_qs(Activity, request.user)

    # ── 组 1：进行中且仍有未完成子活动的活动（一次 prefetch 拉取未完成 children） ──
    pending_children_qs = Activity.objects.filter(
        status__in=['planned', 'in_progress']
    ).order_by('start_date', 'created_at')
    pending_parents = list(exclude_daily_bucket(
        base_qs.filter(
            status='in_progress',
            children__status__in=['planned', 'in_progress'],
        ).distinct().prefetch_related(
            models.Prefetch('children', queryset=pending_children_qs,
                            to_attr='pending_children'),
            'tags',
        ).order_by('-start_date')
    )[:20])

    # ── 组 2：未来 7 天内开始的顶层计划活动 ──
    upcoming = list(exclude_daily_bucket(
        base_qs.filter(
            status='planned',
            parent__isnull=True,
            start_date__gte=today,
            start_date__lte=today + timedelta(days=7),
        ).order_by('start_date', 'created_at').prefetch_related('tags')
    ))
    for a in upcoming:
        a.days_until = (a.start_date - today).days

    return render(request, 'activities/next_actions.html', {
        'pending_parents': pending_parents,
        'upcoming': upcoming,
        'today_display': f'{today.month}月{today.day}日',
    })
