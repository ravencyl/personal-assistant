from datetime import timedelta

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.utils import timezone

from chat.models import Conversation
from activities.models import Activity, Expense
from core.utils import visible_qs


@login_required
def dashboard(request):
    """仪表盘（周报/月报 + 统计卡片）"""
    user = request.user
    today = timezone.localdate()

    # 基础查询
    activities = visible_qs(Activity, user)
    conversations = visible_qs(Conversation, user)

    # ── 本周统计 ──
    week_start = today - timedelta(days=today.weekday())
    week_activities = activities.filter(start_date__gte=week_start, start_date__lte=today)
    week_completed = week_activities.filter(status='done').count()
    week_new = week_activities.count()
    week_expense = Expense.objects.filter(
        user=user, paid_at__gte=week_start, paid_at__lte=today
    ).aggregate(s=Sum('amount'))['s'] or 0

    # ── 本月统计 ──
    month_start = today.replace(day=1)
    month_activities = activities.filter(start_date__gte=month_start)
    month_completed = month_activities.filter(status='done').count()
    month_new = month_activities.count()
    month_expense = Expense.objects.filter(
        user=user, paid_at__gte=month_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    # ── 全局统计 ──
    total_activities = activities.count()
    ongoing_count = activities.filter(status__in=['planned', 'in_progress']).count()
    total_conversations = conversations.count()

    # ── 状态分布 ──
    status_counts = dict(
        activities.values_list('status').annotate(n=Count('id')).values_list('status', 'n')
    )

    # ── 近期活动（最近 10 个） ──
    recent_activities = activities.order_by('-updated_at')[:10]

    # ── 近期对话（最近 5 个） ──
    recent_conversations = conversations.order_by('-updated_at')[:5]

    # ── 本周每日费用（供迷你图表使用） ──
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    daily_expense = [0.0] * 7
    for e in Expense.objects.filter(user=user, paid_at__gte=week_start, paid_at__lte=today):
        if e.paid_at:
            idx = (e.paid_at - week_start).days
            if 0 <= idx < 7:
                daily_expense[idx] += float(e.amount)

    # ── 问候 ──
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
    today_display = f'{today.year}年{today.month}月{today.day}日 · {weekdays[today.weekday()]}'

    stats = {
        'week_completed': week_completed,
        'week_new': week_new,
        'week_expense': float(week_expense),
        'month_completed': month_completed,
        'month_new': month_new,
        'month_expense': float(month_expense),
        'total_activities': total_activities,
        'ongoing_count': ongoing_count,
        'total_conversations': total_conversations,
        'status_counts': status_counts,
    }

    return render(request, 'core/dashboard.html', {
        'recent_conversations': recent_conversations,
        'recent_activities': recent_activities,
        'stats': stats,
        'weekdays': weekdays,
        'daily_expense': daily_expense,
        'today': today,
        'greeting': greeting,
        'today_display': today_display,
    })
