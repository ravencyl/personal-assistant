from django.contrib.contenttypes.models import ContentType
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import timedelta
from urllib.parse import urlencode
from taggit.models import Tag

from .forms import ActivityForm
from .models import Activity, Participant
from core.utils import visible_qs, get_visible


def _user_tag_names(user):
    """可见范围内活动上使用过的全部标签名（供表单 autocomplete 建议）"""
    activity_ids = visible_qs(Activity, user).values('id')
    return list(Tag.objects.filter(
        taggit_taggeditem_items__content_type=ContentType.objects.get_for_model(Activity),
        taggit_taggeditem_items__object_id__in=activity_ids,
    ).distinct().values_list('name', flat=True).order_by('name'))


@login_required
def activity_list(request):
    """活动列表（默认树形结构可折叠，筛选/排序时为平铺列表）"""
    status_filter = request.GET.get('status', '')
    tag_filter = request.GET.get('tag', '').strip()
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

    # 一次性聚合子活动数量，避免 N+1；预取标签（超级用户可见全部数据）
    all_activities = list(visible_qs(Activity, request.user).prefetch_related('tags').annotate(
        sub_count=Count('children', distinct=True),
    ))

    # 筛选条件用于计算命中集合（树形结构始终保留，命中节点及其祖先链可见）
    matched = Activity.objects.filter(id__in=[a.id for a in all_activities])
    if status_filter:
        matched = matched.filter(status=status_filter)
    if tag_filter:
        matched = matched.filter(tags__name=tag_filter)
    # 日期筛选：按活动开始日期是否落在区间内
    if date_from:
        matched = matched.filter(start_date__gte=date_from)
    if date_to:
        matched = matched.filter(start_date__lte=date_to)

    has_filter = bool(status_filter or tag_filter or date_from or date_to)

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

    # 用内存中的 children_map 递归计算累计费用（自身 + 所有后代）
    cost_cache = {}

    def compute_cost(a):
        if a.id not in cost_cache:
            cost_cache[a.id] = (a.cost or 0) + sum(
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
    filters_active = bool(status_filter or tag_filter or date_from or date_to or sort)
    active_filter_count = sum([
        bool(status_filter), bool(tag_filter), bool(date_from or date_to), bool(sort),
    ])

    # 标签筛选需保留状态/日期/排序参数（不含 tag）
    tag_link_params = {k: v for k, v in date_params.items()}
    if status_filter:
        tag_link_params['status'] = status_filter
    if sort:
        tag_link_params['sort'] = sort
    tag_link_qs = urlencode(tag_link_params)

    return render(request, 'activities/activity_list.html', {
        'activities': rows,
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
    """活动详情（含子活动）"""
    activity = get_visible(Activity, request.user, id=activity_id)

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
    activity = get_visible(Activity, request.user, id=activity_id)
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
        'all_tags': _user_tag_names(request.user),
    })


@login_required
def activity_edit(request, activity_id):
    """编辑活动（超级用户编辑他人活动时保持原属主）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    owner = activity.user

    if request.method == 'POST':
        form = ActivityForm(request.POST, instance=activity, user=owner)
        if form.is_valid():
            form.save()
            form.save_participants(activity)
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
        Activity.objects.create(
            user=activity.user,
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
    activity = get_visible(Activity, request.user, id=activity_id)
    name = activity.name
    activity.delete()
    messages.success(request, f'活动「{name}」已删除')
    return redirect('activities:activity_list')
