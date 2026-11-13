"""活动列表页（平铺 + 筛选 + 排序 + 分页 + 置顶）"""
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core.utils import get_visible, visible_qs, week_monday, WEEKDAY_LABELS, greeting

from ..forms import ActivityForm
from ..models import Activity, Participant
from ..services import start_due_activities
from ._common import _user_tag_names
from ..utils import (filter_activities, get_filter_params, exclude_daily_bucket,
                     expense_totals_map)

# 活动列表每页条数（平铺：子任务与顶级活动一样各占一条，2026-11-13 去树形）
ACTIVITY_LIST_PAGE_SIZE = 20


from core.utils import page_window


@login_required
@ensure_csrf_cookie
def activity_list(request):
    """活动列表（平铺：所有活动含子任务各占一行；子任务名字边标记父任务名）"""
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

    # 预取标签（超级用户可见全部数据）
    # 「日常开支」归属桶为系统常驻活动，不展示在活动列表中（费用统计仍包含）
    all_activities = list(exclude_daily_bucket(
        visible_qs(Activity, request.user)
    ).select_related('parent').prefetch_related('tags'))

    # 筛选条件用于计算命中集合（平铺：只留命中行，不补祖先链）
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

    # 每行费用 = 自身 Expense 合计。平铺后子任务独立成行，父行不再累加后代，
    # 否则同一笔钱在父行与子行各出现一次（重复计）。
    expense_totals = expense_totals_map(a.id for a in all_activities)
    for a in all_activities:
        a.show_cost = float(expense_totals.get(a.id, 0) or 0)

    # ── 置顶组：pinned 活动无视筛选/排序/分页，固定展示在列表最前（2026-09-23）──
    # 平铺后置顶就是「若干独立行」（顶级或子任务都一样），最新置顶排最前。
    pinned_rows = sorted(
        (a for a in all_activities if a.pinned),
        key=lambda a: a.pinned_at or a.created_at, reverse=True)
    for a in pinned_rows:
        a.is_pinned = True
    pinned_ids = {a.id for a in pinned_rows}

    # 主列表：筛选时只留命中行（平铺不再补祖先链，子任务命中就单独显示并标父名）；
    # 无筛选时全量（含子任务）。置顶行已上移，主列表剔除避免重复。
    matched_ids = set(matched.values_list('id', flat=True)) if has_filter else None
    rows = [a for a in all_activities
            if a.id not in pinned_ids
            and (matched_ids is None or a.id in matched_ids)]
    for a in rows:
        a.is_pinned = False

    # 平铺排序：空日期始终排最后，费用按自身合计排序
    if sort_field == 'start_date':
        with_val = [a for a in rows if a.start_date is not None]
        nulls = [a for a in rows if a.start_date is None]
        with_val.sort(key=lambda a: a.start_date, reverse=desc)
        rows = with_val + nulls
    elif sort_field == 'cost':
        rows.sort(key=lambda a: a.show_cost or 0, reverse=desc)
    else:
        rows.sort(key=lambda a: getattr(a, sort_field) or '', reverse=desc)

    # ── 分页：平铺后每条活动（含子任务）各占一个名额（2026-11-13 去树形）──
    # 置顶组不参与分页，每页都固定显示在最前
    paginator = Paginator(rows, ACTIVITY_LIST_PAGE_SIZE)
    try:
        page_num = int(request.GET.get('page', ''))
    except (TypeError, ValueError):
        page_num = 1
    if page_num < 1 or page_num > paginator.num_pages:
        page_num = 1  # 非法/越界页码静默回退第 1 页
    page_obj = paginator.page(page_num)
    page_rows = list(page_obj.object_list)

    # 翻页链接基准：保留全部查询参数（含 sort），仅去掉 page
    page_params = request.GET.copy()
    page_params.pop('page', None)
    page_base_qs = page_params.urlencode()

    page_numbers = page_window(page_num, paginator.num_pages)

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

    # 筛选面板默认折叠；URL 带任一筛选/排序参数时自动展开。
    # tag 不算：标签筛选块已下线（2026-10-08），URL 里的 tag 只在折叠条摘要里展示
    filters_active = bool(status_filter or date_from or date_to or sort
                          or participant_filter or keyword_filter)
    active_filter_count = sum([
        bool(status_filter), bool(date_from or date_to), bool(sort),
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
    # tag_link_qs 仍由活动卡上的 #词 链接消费（点标签仍是进筛选的入口），
    # 只是筛选面板里的标签筛选块已下线

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
    greeting_text = greeting()
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
        # 弹窗用的空白表单与 chips 联想数据（字段 partial 与独立创建页共用同一模板）；
        # all_tags 漏传过：标签候选下拉在弹窗里永远是空的，提示文案成了空话（2026-10-08）
        'form': ActivityForm(user=request.user),
        'all_participants': list(visible_qs(Participant, request.user).values_list('name', flat=True)),
        'all_tags': _user_tag_names(request.user),
        'greeting': greeting_text,
        'today_display': today_display,
        'ongoing_count': ongoing_count,
        'today_count': today_count,
        'status_filter': status_filter,
        'status_choices': Activity.STATUS_CHOICES,
        'tag_filter': tag_filter,
        'keyword_filter': keyword_filter,
        'match_count': len(matched_ids) if has_filter else 0,
        'search_clear_qs': search_clear_qs,
        'tag_link_qs': tag_link_qs,
        'filters_active': filters_active,
        'active_filter_count': active_filter_count,
        'active_filters': active_filters,
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
