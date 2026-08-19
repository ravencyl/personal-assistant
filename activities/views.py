from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import timedelta
from urllib.parse import urlencode

from .forms import ActivityForm
from .models import Activity, Participant


@login_required
def activity_list(request):
    """活动列表（默认树形结构可折叠，筛选/排序时为平铺列表）"""
    status_filter = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    sort = request.GET.get('sort', '').strip()

    # 表头排序字段映射（key 为 URL 参数值，value 为模型/注解字段）
    sort_fields = {
        'name': 'name',
        'status': 'status',
        'start_date': 'start_date',
        'cost': 'cost',
        'sub_count': 'sub_count',
    }

    # 一次性聚合子活动数量，避免 N+1；预取标签
    activities = Activity.objects.filter(user=request.user).prefetch_related('tags').annotate(
        sub_count=Count('children', distinct=True),
    )

    if status_filter:
        activities = activities.filter(status=status_filter)
    # 日期筛选：按活动开始日期是否落在区间内
    if date_from:
        activities = activities.filter(start_date__gte=date_from)
    if date_to:
        activities = activities.filter(start_date__lte=date_to)

    has_filter = bool(status_filter or date_from or date_to)

    # 排序：校验字段合法性，日期/费用类字段空值排最后
    order_expr = None
    sort_key = sort.lstrip('-')
    if sort_key in sort_fields:
        desc = sort.startswith('-')
        field = sort_fields[sort_key]
        if field in ('start_date', 'cost'):
            order_expr = F(field).desc(nulls_last=True) if desc else F(field).asc(nulls_last=True)
        else:
            order_expr = ('-' + field) if desc else field
    else:
        sort = ''

    if has_filter or sort:
        # 筛选/排序时平铺展示（避免树被截断或打乱层级）
        rows = list(activities.order_by(order_expr)) if order_expr else list(activities)
        tree_mode = False
        # 展示累计费用（自身 + 所有后代），个人数据量小，递归查询可接受
        for a in rows:
            a.show_cost = a.total_cost
    else:
        # 深度优先遍历构建活动树，为每行附加 depth 层级
        children_map = {}
        for a in activities:
            children_map.setdefault(a.parent_id, []).append(a)

        rows = []

        def walk(parent_id, depth):
            for a in children_map.get(parent_id, []):
                a.depth = depth
                a.has_children = bool(children_map.get(a.id))
                rows.append(a)
                walk(a.id, depth + 1)

        walk(None, 0)
        tree_mode = True

        # 树形模式下用内存中的 children_map 递归计算累计费用，避免重复查库
        cost_cache = {}

        def compute_cost(a):
            if a.id not in cost_cache:
                cost_cache[a.id] = (a.cost or 0) + sum(
                    compute_cost(c) for c in children_map.get(a.id, [])
                )
            return cost_cache[a.id]

        for a in rows:
            a.show_cost = compute_cost(a)

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

    return render(request, 'activities/activity_list.html', {
        'activities': rows,
        'status_filter': status_filter,
        'status_choices': Activity.STATUS_CHOICES,
        'tree_mode': tree_mode,
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
    """活动详情（含子活动）"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)

    return render(request, 'activities/activity_detail.html', {
        'activity': activity,
        'children': activity.children.all(),
        'participants': activity.participants.all(),
        'status_choices': Activity.STATUS_CHOICES,
    })


@login_required
@require_POST
def activity_set_status(request, activity_id):
    """快捷修改活动状态"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)
    status = request.POST.get('status', '')
    valid = dict(Activity.STATUS_CHOICES)
    if status not in valid:
        messages.error(request, '无效的状态值')
    elif status != activity.status:
        activity.status = status
        activity.save(update_fields=['status', 'updated_at'])
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
            form.save_children(activity)
            messages.success(request, f'活动「{activity.name}」已创建')
            return redirect('activities:activity_detail', activity.id)
    else:
        form = ActivityForm(user=request.user)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '新建活动',
        'all_participants': list(Participant.objects.filter(user=request.user).values_list('name', flat=True)),
    })


@login_required
def activity_edit(request, activity_id):
    """编辑活动"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)

    if request.method == 'POST':
        form = ActivityForm(request.POST, instance=activity, user=request.user)
        if form.is_valid():
            form.save()
            form.save_participants(activity)
            messages.success(request, f'活动「{activity.name}」已更新')
            return redirect('activities:activity_detail', activity.id)
    else:
        form = ActivityForm(instance=activity, user=request.user)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '编辑活动',
        'activity': activity,
        'children': activity.children.all(),
        'all_participants': list(Participant.objects.filter(user=request.user).values_list('name', flat=True)),
    })


@login_required
@require_POST
def add_subactivity(request, activity_id):
    """快捷创建子活动（仅填名称）"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, '子活动名称不能为空')
    else:
        Activity.objects.create(
            user=request.user,
            name=name,
            parent=activity,
            end_date=timezone.localdate(),
        )
        messages.success(request, f'子活动「{name}」已创建')
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def activity_delete(request, activity_id):
    """删除活动"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)
    name = activity.name
    activity.delete()
    messages.success(request, f'活动「{name}」已删除')
    return redirect('activities:activity_list')
