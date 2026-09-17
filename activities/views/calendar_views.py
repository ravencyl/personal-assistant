"""活动日历：月/周/日视图页面 + 数据 API + ICS 订阅源"""
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.http import HttpResponse, Http404, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone

from core.tags import tag_names
from core.utils import visible_qs, week_monday, WEEKDAY_SHORT

from ..models import Activity


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
        monday = week_monday(ref_date)
        sunday = monday + timedelta(days=6)
        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            days.append({
                'day': d.day,
                'weekday': WEEKDAY_SHORT[i],
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
        weekdays_cn = WEEKDAY_SHORT
        title = f'{d.month}月{d.day}日 周{weekdays_cn[d.weekday()]}'
        ctx = {'hours': hours, 'day_date': d.isoformat(), 'today_iso': today.isoformat()}

    # Select Date 星期条（截图第二屏同构）：ref_date 所在周 7 天，
    # 今天 accent 实心、当前查看日描边，点击进入该天日视图
    strip_monday = week_monday(ref_date)
    week_strip = []
    for i in range(7):
        d = strip_monday + timedelta(days=i)
        week_strip.append({
            'day': d.day,
            'weekday': WEEKDAY_SHORT[i],
            'is_today': d == today,
            'is_selected': d == ref_date,
            'date_str': d.isoformat(),
        })

    prev_params = {'mode': mode, 'date': prev_date.isoformat()}
    next_params = {'mode': mode, 'date': next_date.isoformat()}
    today_params = {'mode': mode, 'date': today.isoformat()}

    return render(request, 'activities/activity_calendar.html', {
        **ctx,
        'mode': mode,
        'title': title,
        'weekdays': WEEKDAY_SHORT,
        'week_strip': week_strip,
        # 图例与色块同源：选项与顺序取 STATUS_CHOICES，颜色取 custom.css 的 --status-*
        'status_choices': Activity.STATUS_CHOICES,
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
            monday = week_monday(today)
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

    # 颜色不在这层定义：前端直接按 status 取 custom.css 的 var(--status-<status>)
    data = []
    for a in activities:
        data.append({
            'id': a.id,
            'name': a.name,
            'start_date': a.start_date.isoformat(),
            'end_date': (a.end_date or a.start_date).isoformat(),
            'status': a.status,
            'status_label': a.get_status_display(),
            'url': reverse('activities:activity_detail', args=[a.id]),
            'tags': tag_names(a),
        })

    return JsonResponse({'activities': data})


# ── 日历订阅（ICS/webcal feed，2026-09-11）─────────────────────────────

def calendar_feed(request, token):
    """ICS 订阅端点：token 即鉴权（Apple 日历服务器代拉取，无法带会话）。

    token 不存在 / 已吊销一律 404（不区分「不存在」与「已吊销」，
    不向探测者泄露令牌状态）。本视图刻意不加 login_required。
    """
    from ..models import CalendarFeed
    from ..calendar_feed import build_ics

    feed = (CalendarFeed.objects
            .filter(token=token, revoked_at__isnull=True)
            .select_related('user')
            .first())
    if feed is None:
        raise Http404('日历订阅不存在')

    ics = build_ics(feed.user, request.build_absolute_uri('/'))
    response = HttpResponse(ics, content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = 'inline; filename="activities.ics"'
    response['Cache-Control'] = 'no-store'
    return response


@login_required
def calendar_feed_settings(request):
    """日历订阅管理页：查看订阅 URL、重新生成 / 吊销令牌。"""
    from ..models import CalendarFeed

    feed = CalendarFeed.issue(request.user)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'regenerate':
            feed.regenerate()
            messages.success(request, '已生成新的订阅链接，旧链接立即失效。')
            return redirect('activities:calendar_feed_settings')
        if action == 'revoke':
            feed.revoke()
            messages.success(request, '订阅已吊销，原链接无法再拉取数据。')
            return redirect('activities:calendar_feed_settings')

    # calendar_feed_ics 注册在根 URLconf（无 namespace），供 Apple 日历直接订阅
    feed_url = request.build_absolute_uri(
        reverse('calendar_feed_ics', args=[feed.token]))
    webcal_url = feed_url.replace('https://', 'webcal://').replace('http://', 'webcal://')
    return render(request, 'activities/calendar_feed_settings.html', {
        'feed': feed,
        'feed_url': feed_url,
        'webcal_url': webcal_url,
    })
