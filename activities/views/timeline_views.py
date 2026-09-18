"""活动时间线视图：按日分组、倒序、分页"""
from collections import OrderedDict
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone

from core.utils import visible_qs, WEEKDAY_LABELS

from ..models import Activity
from ..utils import exclude_daily_bucket
from ._common import attach_costs


@login_required
def timeline_view(request):
    """活动时间线：按日分组展示所有活动，倒序排列，支持分页

    分组逻辑：以 start_date 为主键（无 start_date 时退回 created_at 的日期部分），
    同一天内的活动按 created_at 倒序。页面每次展示 14 天（约两周），
    翻页加载更早的内容。
    """
    today = timezone.localdate()
    base_qs = visible_qs(Activity, request.user).select_related('parent').prefetch_related('tags', 'participants')
    base_qs = exclude_daily_bucket(base_qs)

    # 按 start_date 倒序（无日期的排最后）；同日期按 created_at 倒序
    qs = base_qs.order_by('-start_date', '-created_at')

    # 分页：按天分组后分页（每页 14 天）
    # 先取所有活动，按日期分组，再手动分页
    activities = list(qs[:500])  # 上限 500 条，避免内存爆炸
    attach_costs(activities)

    # 按日期分组
    groups = OrderedDict()
    for activity in activities:
        day = activity.start_date or activity.created_at.date()
        if day not in groups:
            groups[day] = []
        groups[day].append(activity)

    # 转为列表以便分页（每个元素是一天的数据）
    page_items = []
    for day, day_activities in groups.items():
        # 日期标签
        delta = (today - day).days
        if delta == 0:
            label = '今天'
        elif delta == 1:
            label = '昨天'
        elif delta < 7:
            label = f'{WEEKDAY_LABELS[day.weekday()]}'
        else:
            label = f'{day.month}月{day.day}日'

        weekday = WEEKDAY_LABELS[day.weekday()]
        page_items.append({
            'date': day,
            'label': label,
            'weekday': weekday,
            'activities': day_activities,
            'count': len(day_activities),
        })

    # 手动分页：每页 14 天
    paginator = Paginator(page_items, 14)
    page_num = request.GET.get('page', 1)
    try:
        page_num = int(page_num)
    except (ValueError, TypeError):
        page_num = 1
    page_obj = paginator.get_page(page_num)

    # 统计信息
    total_count = len(activities)
    done_count = sum(1 for a in activities if a.status == 'done')

    return render(request, 'activities/timeline.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'today': today,
        'total_count': total_count,
        'done_count': done_count,
    })
