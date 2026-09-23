"""活动列表页（树形 + 筛选 + 排序 + 分页 + 置顶）"""
import json
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core.utils import get_visible, visible_qs, week_monday, WEEKDAY_LABELS

from ..forms import ActivityForm
from ..models import Activity, Participant
from ..services import start_due_activities
from ..utils import (filter_activities, get_filter_params, exclude_daily_bucket,
                     expense_totals_map)
from ._common import _user_tag_names, _greeting

# 活动列表每页顶级活动数（子活动跟随父活动，不计入）
ACTIVITY_LIST_PAGE_SIZE = 20
# 筛选面板标签区默认可见数（激活的标签不受此限）
_TAGS_VISIBLE = 10


def _page_window(current, total, span=2):
    """页码收敛窗口：当前页 ±span + 首末页；页数少时全部展示，断口以 None 占位（渲染省略号）"""
    if total <= span * 2 + 3:
        return list(range(1, total + 1))
    left = max(2, current - span)
    right = min(total - 1, current + span)
    items = [1]
    if left > 2:
        items.append(None)
    items.extend(range(left, right + 1))
    if right < total - 1:
        items.append(None)
    items.append(total)
    return items


@login_required
@ensure_csrf_cookie
def activity_list(request):
    """活动列表（默认树形结构可折叠，筛选/排序时为平铺列表）"""
    # 到期活动自动转进行中（与 cron 同一份判定，见 services.start_due_activities）
    start_due_activities(request.user)

    filters = get_filter_params(request)
    status_filter = filters['status']
    tag_filter = filters['tag']
    date_from = filters['date_from']
    date_to = filters['date_to']
    participant_filter = filters['participant']
    keyword_filter = filters['keyword']
    sort = request.GET.get('sort', '').strip()

    # 表头排序字段映射（key 为 URL 参数值，value 为模型/注解字段）
    sort_fields = {
        'name': 'name',
        'status': 'status',
        'start_date': 'start_date',
        'cost': 'cost',
    }

    # 一次性聚合子活动数量，避免 N+1；预取标签（超级用户可见全部数据）
    # 「日常开支」归属桶为系统常驻活动，不展示在活动列表中（费用统计仍包含）
    # （笔数/子活动列已下线，sub_count 仅剩费用单元格的「含子活动」标注在用）
    all_activities = list(exclude_daily_bucket(
        visible_qs(Activity, request.user)
    ).prefetch_related('tags').annotate(
        sub_count=Count('children', distinct=True),
    ))

    # 筛选条件用于计算命中集合（树形结构始终保留，命中节点及其祖先链可见）
    matched = exclude_daily_bucket(filter_activities(request.user, filters))

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
    expense_totals = expense_totals_map(a.id for a in all_activities)

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

    # ── 置顶组：pinned 活动无视筛选/排序/分页，固定展示在列表最前（2026-09-23）──
    # 顶级置顶活动连同其后代树整体上移（后代跟父不留在树区）；置顶的子活动
    # 独立提取到顶部（depth 归 0），不再出现在其父的树里。
    pinned_rows = []

    def walk_group(root, depth):
        """置顶组整棵子树都标 is_pinned（渲染同一行模板，整组高亮）"""
        root.depth = depth
        root.has_children = bool(children_map.get(root.id))
        root.is_pinned = True
        pinned_rows.append(root)
        for child in children_map.get(root.id, []):
            walk_group(child, depth + 1)

    # 顶级置顶（连同后代树），最新的置顶排最前
    pinned_tops = sorted(
        (a for a in children_map.get(None, []) if a.pinned),
        key=lambda a: a.pinned_at or a.created_at, reverse=True)
    for top in pinned_tops:
        walk_group(top, 0)

    # 置顶的子活动：从父树里挖出来单独置顶（最新置顶在前）
    pinned_subs = []
    for parent_id, siblings in list(children_map.items()):
        if parent_id is None:
            continue
        extracted = [a for a in siblings if a.pinned]
        if extracted:
            children_map[parent_id] = [a for a in siblings if not a.pinned]
            pinned_subs.extend(sorted(
                extracted, key=lambda a: a.pinned_at or a.created_at, reverse=True))
    for sub in pinned_subs:
        walk_group(sub, 0)

    pinned_ids = {a.id for a in pinned_rows}

    # 树区：剔除置顶组（整组上移了，不能两处重复出现）
    def strip_pinned(parent_id, depth):
        kept = []
        for a in children_map.get(parent_id, []):
            if a.id in pinned_ids:
                continue
            a.depth = depth
            a.has_children = bool(children_map.get(a.id))
            a.is_pinned = False
            kept.append(a)
            kept.extend(strip_pinned(a.id, depth + 1))
        return kept

    rows = strip_pinned(None, 0)
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

    # ── 分页：按「顶级活动」分页，子活动跟随父活动同页、不计入每页条数（2026-09-13）──
    # 置顶组不参与分页，每页都固定显示在最前
    top_groups = []
    for a in rows:
        if not a.depth:
            top_groups.append([a])
        else:
            top_groups[-1].append(a)
    paginator = Paginator(top_groups, ACTIVITY_LIST_PAGE_SIZE)
    try:
        page_num = int(request.GET.get('page', ''))
    except (TypeError, ValueError):
        page_num = 1
    if page_num < 1 or page_num > paginator.num_pages:
        page_num = 1  # 非法/越界页码静默回退第 1 页
    page_obj = paginator.page(page_num)
    page_rows = [a for group in page_obj.object_list for a in group]
    # 置顶组不进主列表，单独传给模板上方的「已置顶」区域（2026-09-23）

    # 翻页链接基准：保留全部查询参数（含 sort），仅去掉 page
    page_params = request.GET.copy()
    page_params.pop('page', None)
    page_base_qs = page_params.urlencode()

    page_numbers = _page_window(page_num, paginator.num_pages)

    # 快捷日期片高亮判断（今天/本周为新增片，7d/30d 沿用原有口径）
    today = timezone.localdate()
    quick = ''
    week_from = week_monday(today)
    week_to = week_from + timedelta(days=6)
    if date_from and date_to:
        if (date_from, date_to) == (str(today), str(today)):
            quick = 'today'
        elif (date_from, date_to) == (str(week_from), str(week_to)):
            quick = 'week'
        elif (date_from, date_to) == (str(today - timedelta(days=6)), str(today)):
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
    all_tags = _user_tag_names(request.user)

    # 激活筛选摘要（筛选折叠条上直接展示，每项可单独一键移除）。
    # 之前折叠时只给「N 项生效」，用户不知道生效的是哪几项、要展开再找再清（可用性差）。
    # remove_qs = 当前全部参数去掉该项（分页重置，其余筛选保留）。
    base_params = {k: v for k, v in request.GET.items() if k != 'page'}

    def _qs_without(*keys):
        params = {k: v for k, v in base_params.items() if k not in keys}
        return ('?' + urlencode(params)) if params else ''

    status_labels = dict(Activity.STATUS_CHOICES)
    active_filters = []
    if date_from or date_to:
        active_filters.append({
            'label': f'日期 {date_from or "…"} ~ {date_to or "…"}',
            'remove_qs': _qs_without('date_from', 'date_to'),
        })
    if status_filter:
        active_filters.append({
            'label': f'状态：{status_labels.get(status_filter, status_filter)}',
            'remove_qs': _qs_without('status'),
        })
    if tag_filter:
        active_filters.append({
            'label': f'# {tag_filter}',
            'remove_qs': _qs_without('tag'),
        })
    if participant_filter:
        active_filters.append({
            'label': f'参与者 {participant_filter}',
            'remove_qs': _qs_without('participant'),
        })
    if keyword_filter:
        active_filters.append({
            'label': f'关键词「{keyword_filter}」',
            'remove_qs': _qs_without('keyword'),
        })
    if sort:
        active_filters.append({
            'label': '排序 ' + ('↓' if sort.startswith('-') else '↑'),
            'remove_qs': _qs_without('sort'),
        })

    # 首页问候头部：时段问候 + 日期星期 + 今日摘要
    greeting = _greeting()
    weekdays = WEEKDAY_LABELS
    today_display = f'{today.month}月{today.day}日 · {weekdays[today.weekday()]}'
    ongoing_count = sum(1 for a in all_activities if a.status == 'in_progress')
    today_count = sum(
        1 for a in all_activities
        if (a.start_date and a.start_date <= today and (a.end_date is None or a.end_date >= today))
        or (a.start_date is None and a.end_date == today)
    )

    return render(request, 'activities/activity_list.html', {
        'activities': page_rows,
        'pinned_activities': pinned_rows,
        'page_obj': page_obj,
        'page_numbers': page_numbers,
        'total_activities': len(rows),
        'page_base_qs': page_base_qs,
        # 弹窗用的空白表单与 chips 联想数据（字段 partial 与独立创建页共用同一模板）
        'form': ActivityForm(user=request.user),
        'all_participants': list(visible_qs(Participant, request.user).values_list('name', flat=True)),
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
        'all_tags': all_tags,
        # 溢出数在服务端算好（模板 |add 对字符串是拼接，不能拿来做算术）
        'TAGS_VISIBLE': _TAGS_VISIBLE,
        'tag_overflow': max(len(all_tags) - _TAGS_VISIBLE, 0),
        'tag_link_qs': tag_link_qs,
        'filters_active': filters_active,
        'active_filter_count': active_filter_count,
        'active_filters': active_filters,
        'tree_mode': True,
        'expand_all': expand_all,
        'date_from': date_from,
        'date_to': date_to,
        'quick': quick,
        'quick_7d_from': str(today - timedelta(days=6)),
        'quick_30d_from': str(today - timedelta(days=29)),
        'week_from': str(week_from),
        'week_to': str(week_to),
        'today_str': str(today),
        'sort': sort,
        'filter_qs': filter_qs,
        'date_qs': date_qs,
    })


@login_required
@require_POST
def activity_pin_toggle(request, activity_id):
    """置顶/取消置顶（JSON）：置顶活动无视筛选条件固定在列表最前（2026-09-23）

    状态存在 Activity.pinned / pinned_at（pinned_at 决定多个置顶间的先后）。
    原生 fetch 专用端点：越权/不存在走 get_visible 的统一 404。
    """
    activity = get_visible(Activity, request.user, id=activity_id)
    activity.pinned = not activity.pinned
    activity.pinned_at = timezone.now() if activity.pinned else None
    activity.save(update_fields=['pinned', 'pinned_at'])
    return JsonResponse({'id': activity.id, 'pinned': activity.pinned})
