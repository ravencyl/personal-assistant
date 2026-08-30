from datetime import date, timedelta

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import JsonResponse

from chat.models import Conversation
from activities.models import Activity, Expense
from core.utils import visible_qs, week_monday, daily_totals, WEEKDAY_LABELS
from core.search import global_search
from core.report_generator import collect_report_data, generate_report, save_report_to_knowledge
from knowledge.models import Article


@login_required
def dashboard(request):
    """仪表盘（周报/月报 + 统计卡片）"""
    user = request.user
    today = timezone.localdate()

    # 基础查询
    activities = visible_qs(Activity, user)
    conversations = visible_qs(Conversation, user)

    # ── 本周统计 ──
    week_start = week_monday(today)
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
    weekdays = WEEKDAY_LABELS
    daily_expense = daily_totals(
        Expense.objects.filter(user=user, paid_at__gte=week_start, paid_at__lte=today),
        week_start,
    )

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


@login_required
def search_api(request):
    """全局搜索 API（返回 HTML 片段供 HTMX 使用）"""
    query = request.GET.get('q', '').strip()
    if not query:
        return render(request, 'core/_search_panel.html', {'query': '', 'results': None})

    results = global_search(request.user, query)

    # 计算总结果数
    total = sum(
        len(v) if v and not isinstance(v[0], tuple) else len(v)
        for v in results.values() if v
    )

    return render(request, 'core/_search_panel.html', {
        'query': query,
        'results': results,
        'total': total,
    })


@login_required
def weekly_report(request):
    """生成/查看本周周报"""
    user = request.user
    today = timezone.localdate()
    week_start = week_monday(today)
    week_end = today

    # 检查是否已有本周报告
    existing = Article.objects.filter(
        user=user,
        tags__name='report-weekly',
        created_at__gte=timezone.make_aware(
            timezone.datetime.combine(week_start, timezone.datetime.min.time())
        ),
    ).first()

    if request.method == 'POST' or (not existing and request.GET.get('generate')):
        markdown, data = generate_report(user, 'weekly', week_start, week_end)
        title = f'周报 · {today.year}年第{week_start.isocalendar()[1]}周 ({week_start.strftime("%m.%d")}-{week_end.strftime("%m.%d")})'

        if request.method == 'POST':
            article = save_report_to_knowledge(user, 'weekly', title, markdown)
            return redirect('knowledge:article_detail', slug=article.slug)

        return render(request, 'core/weekly_report.html', {
            'report_type': 'weekly',
            'title': title,
            'markdown': markdown,
            'data': data,
            'week_start': week_start,
            'week_end': week_end,
        })

    if existing:
        return redirect('knowledge:article_detail', slug=existing.slug)

    # GET 且无报告：显示预览
    markdown, data = generate_report(user, 'weekly', week_start, week_end)
    title = f'周报 · {today.year}年第{week_start.isocalendar()[1]}周 ({week_start.strftime("%m.%d")}-{week_end.strftime("%m.%d")})'

    return render(request, 'core/weekly_report.html', {
        'report_type': 'weekly',
        'title': title,
        'markdown': markdown,
        'data': data,
        'week_start': week_start,
        'week_end': week_end,
    })


@login_required
def monthly_report(request):
    """生成/查看本月月报"""
    user = request.user
    today = timezone.localdate()
    month_start = today.replace(day=1)
    month_end = today

    existing = Article.objects.filter(
        user=user,
        tags__name='report-monthly',
        created_at__gte=timezone.make_aware(
            timezone.datetime.combine(month_start, timezone.datetime.min.time())
        ),
    ).first()

    if request.method == 'POST' or (not existing and request.GET.get('generate')):
        markdown, data = generate_report(user, 'monthly', month_start, month_end)
        title = f'月报 · {today.year}年{today.month}月'

        if request.method == 'POST':
            article = save_report_to_knowledge(user, 'monthly', title, markdown)
            return redirect('knowledge:article_detail', slug=article.slug)

        return render(request, 'core/weekly_report.html', {
            'report_type': 'monthly',
            'title': title,
            'markdown': markdown,
            'data': data,
            'month_start': month_start,
            'month_end': month_end,
        })

    if existing:
        return redirect('knowledge:article_detail', slug=existing.slug)

    markdown, data = generate_report(user, 'monthly', month_start, month_end)
    title = f'月报 · {today.year}年{today.month}月'

    return render(request, 'core/weekly_report.html', {
        'report_type': 'monthly',
        'title': title,
        'markdown': markdown,
        'data': data,
        'month_start': month_start,
        'month_end': month_end,
    })


@login_required
def yearly_report(request):
    """生成/查看年度回顾报告（年份缺省当年，可查往年）"""
    user = request.user
    today = timezone.localdate()

    try:
        year = int(request.POST.get('year') or request.GET.get('year') or today.year)
    except (TypeError, ValueError):
        year = today.year
    if year < 2000 or year > today.year:
        year = today.year

    period_start = date(year, 1, 1)
    period_end = date(year, 12, 31) if year < today.year else today
    title = f'年报 · {year}年'

    # 查重：同一年份的年报只保留一份（按标题匹配，兼容跨年生成往年报告）
    existing = Article.objects.filter(
        user=user,
        tags__name='report-yearly',
        title=title,
    ).first()

    context = {
        'report_type': 'yearly',
        'title': title,
        'year': year,
        'month_start': period_start,
        'month_end': period_end,
    }

    if request.method == 'POST' or (not existing and request.GET.get('generate')):
        markdown, data = generate_report(user, 'yearly', period_start, period_end)
        context.update({'markdown': markdown, 'data': data})

        if request.method == 'POST':
            article = save_report_to_knowledge(user, 'yearly', title, markdown)
            return redirect('knowledge:article_detail', slug=article.slug)

        return render(request, 'core/weekly_report.html', context)

    if existing:
        return redirect('knowledge:article_detail', slug=existing.slug)

    markdown, data = generate_report(user, 'yearly', period_start, period_end)
    context.update({'markdown': markdown, 'data': data})
    return render(request, 'core/weekly_report.html', context)


@login_required
@require_POST
def report_send_to_chat(request):
    """推送报告摘要到对话"""
    content = request.POST.get('content', '').strip()
    title = request.POST.get('title', '报告').strip()

    if not content:
        return JsonResponse({'error': '报告内容不能为空'}, status=400)

    # 找到或创建对话
    from chat.models import Conversation, Message
    conversation = Conversation.objects.filter(
        user=request.user, status='idle'
    ).order_by('-updated_at').first()

    if not conversation:
        return JsonResponse({'error': '没有可用的对话'}, status=400)

    # 创建消息
    Message.objects.create(
        conversation=conversation,
        role='assistant',
        content=f'📊 {title}\n\n{content[:500]}{"..." if len(content) > 500 else ""}',
        event_type='assistant.message',
        payload={
            'card': 'report',
            'card_data': {
                'title': title,
                'summary': content[:200],
            },
        },
    )

    return JsonResponse({'success': True, 'conversation_id': conversation.id})
