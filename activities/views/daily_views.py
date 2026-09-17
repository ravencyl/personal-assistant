"""Daily 每日简报页与「下一步行动」页"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db import models
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from core.utils import visible_qs, week_monday, WEEKDAY_LABELS

from ..forms import ActivityForm
from ..models import Activity, Expense, Participant
from ..utils import exclude_daily_bucket
from ._common import _user_tag_names, _greeting, attach_costs


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
    # 与 ongoing 同口径排掉 done：今日已完成的事归入「近期完成」，不占用「今日进行中/今日结束」
    starting_today = list(qs.filter(start_date=today).exclude(
        status='cancelled'
    ).exclude(
        status='done'
    ).exclude(id__in=ongoing_ids))
    ending_today = list(qs.filter(end_date=today).exclude(
        status='cancelled'
    ).exclude(
        status='done'
    ).exclude(id__in=ongoing_ids).exclude(
        id__in=[a.id for a in starting_today]
    ))

    # ── 即将开始（未来 7 天） ──
    upcoming = list(qs.filter(
        start_date__gt=today,
        start_date__lte=today + timedelta(days=7),
    ).exclude(status='cancelled').order_by('start_date')[:10])

    # ── 近期完成（最近 3 天）：end_date 为空时退回 start_date，
    # 否则「今天开始并已打卡、没填结束日期」的活动三个区都进不去，会直接消失
    recently_done = list(qs.filter(
        status='done',
    ).filter(
        models.Q(end_date__gte=today - timedelta(days=3)) |
        models.Q(end_date__isnull=True, start_date__gte=today - timedelta(days=3))
    ).order_by('-end_date', '-start_date')[:10])

    # ── 进行中（全局，排除「日常开支」归属桶） ──
    in_progress = list(exclude_daily_bucket(qs.filter(status='in_progress')).exclude(
        id__in=[a.id for a in ongoing]
    ).order_by('-start_date')[:10])

    # ── 统计：今日实际消费 / 本周消费（按 paid_at 筛选）──
    # 这两个是「我花了多少」的个人指标，故意只算本人费用（不走 visible_qs）；
    # 页面上的活动列表则统一跟 visible_qs 口径。参见 AGENTS.md「两个可见性口径」。
    today_expense = Expense.objects.filter(
        user=request.user,
        paid_at=today,
    ).aggregate(s=Sum('amount'))['s'] or 0

    this_week_start = week_monday(today)
    this_week_expense = Expense.objects.filter(
        user=request.user,
        paid_at__gte=this_week_start,
    ).aggregate(s=Sum('amount'))['s'] or 0

    # 问候 + 日期星期
    greeting = _greeting()
    weekdays = WEEKDAY_LABELS
    today_display = f'{today.year}年{today.month}月{today.day}日 · {weekdays[today.weekday()]}'

    # 六个分组互斥，合并后一次 attach_costs：
    # 原先每组各发 2 条聚合（共 12 条），现在固定 2 条
    attach_costs([*ongoing, *starting_today, *ending_today,
                  *upcoming, *recently_done, *in_progress])

    return render(request, 'activities/daily.html', {
        'today': today,
        'today_display': today_display,
        'greeting': greeting,
        # 新建活动弹窗（与列表页共用 partial）：空白表单 + chips 联想数据
        'form': ActivityForm(user=request.user),
        'all_participants': list(visible_qs(Participant, request.user).values_list('name', flat=True)),
        'all_tags': _user_tag_names(request.user),
        'ongoing': ongoing,
        'starting_today': starting_today,
        'ending_today': ending_today,
        'upcoming': upcoming,
        'recently_done': recently_done,
        'in_progress': in_progress,
        'today_expense': float(today_expense),
        'this_week_expense': float(this_week_expense),
        'ongoing_count': len(ongoing) + len(starting_today),
        'in_progress_count': exclude_daily_bucket(qs).filter(status='in_progress').count(),
    })


@login_required
def next_actions(request):
    """下一步行动：未来 7 天内开始的计划活动，按日期排序（只读）

    子活动与顶层活动同口径混排（用户定策：子任务也是活动，一样处理，
    不再单独设「待处理的子任务」分组）；卡片对子活动标注归属父活动。
    「日常开支」等系统归属桶统一排除。
    """
    today = timezone.localdate()
    base_qs = visible_qs(Activity, request.user)

    # 子活动与顶层活动同口径处理（用户定策：子任务也是活动，不单独分组），
    # 按开始日期自然混排；select_related('parent') 供卡片展示归属父活动
    upcoming = list(exclude_daily_bucket(
        base_qs.filter(
            status='planned',
            start_date__gte=today,
            start_date__lte=today + timedelta(days=7),
        ).order_by('start_date', 'created_at')
         .select_related('parent').prefetch_related('tags')
    ))
    for a in upcoming:
        a.days_until = (a.start_date - today).days

    return render(request, 'activities/next_actions.html', {
        'upcoming': upcoming,
        'today_display': f'{today.month}月{today.day}日',
    })
