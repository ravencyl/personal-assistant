"""Daily 数据采集层与「下一步行动」页（daily 页面已于 2026-09-19 下线，/daily/ 重定向回对话）"""
from datetime import timedelta
import logging

from django.contrib.auth.decorators import login_required
from django.db import models
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from core.utils import visible_qs, week_monday, WEEKDAY_LABELS

from ..models import Activity, Expense
from ..utils import exclude_daily_bucket
from ._common import attach_costs

logger = logging.getLogger(__name__)


def _annotate_blocked(activities):
    """标注活动的阻塞状态：is_blocked + blocking_names"""
    for a in activities:
        a.is_blocked = a.is_blocked()
        a.blocking_names = a.blocking_names()
    return activities


def _detect_conflicts(activities):
    """检测时间冲突：同一天有多个带时间的活动（全天活动不参与冲突检测）

    返回冲突活动 ID 集合。无冲突返回空集。
    """
    conflict_ids = set()
    # 只检查有 start_time 的活动（全天活动不参与时间冲突）
    timed = [a for a in activities if a.start_time]
    for i, a in enumerate(timed):
        for b in timed[i+1:]:
            # 简单冲突判定：同一天且时间段重叠
            a_start = a.start_time
            a_end = a.end_time or a.start_time
            b_start = b.start_time
            b_end = b.end_time or b.start_time
            # 时间段重叠条件：a_start < b_end AND b_start < a_end
            if a_start < b_end and b_start < a_end:
                conflict_ids.add(a.id)
                conflict_ids.add(b.id)
    return conflict_ids


def gather_daily(user):
    """Daily 页与对话里的 daily 简报卡共用的数据采集层（2026-09-19 从 daily_view 抽出）

    只查数据，不碰纯展示件（问候语、新建表单等仍归 daily_view）。
    可见性口径照旧：活动列表走 visible_qs；花费合计是「我花了多少」的
    个人指标，故意只算本人（AGENTS.md「两个可见性口径」）。
    返回 dict，键与 daily_view 的模板上下文一一对应。
    """
    today = timezone.localdate()
    qs = visible_qs(Activity, user).prefetch_related('tags', 'participants')

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
    today_expense = float(Expense.objects.filter(
        user=user,
        paid_at=today,
    ).aggregate(s=Sum('amount'))['s'] or 0)

    this_week_start = week_monday(today)
    this_week_expense = float(Expense.objects.filter(
        user=user,
        paid_at__gte=this_week_start,
    ).aggregate(s=Sum('amount'))['s'] or 0)

    # ── 过期未完成自动滚入：status='planned' 且 start_date < today ──
    overdue_rolled_in = list(qs.filter(
        status='planned',
        start_date__lt=today,
    ).order_by('start_date')[:10])
    for a in overdue_rolled_in:
        a.overdue_days = (today - a.start_date).days

    # ── 冲突检测：今日进行中 + 今日开始的活动 ──
    conflict_ids = _detect_conflicts([*ongoing, *starting_today])

    # ── AI 今日建议：读 cron 预计算缓存，无缓存时快速规则降级（不调 AI） ──
    from ..models import DailySuggestion
    cached = DailySuggestion.objects.filter(user=user, date=today).first()
    if cached:
        ai_suggestion = cached.suggestion
        ai_suggestion_is_ai = cached.is_ai
    else:
        # cron 还没跑过或今天刚过零点，用规则快速降级（不阻塞页面）
        ai_suggestion = None
        ai_suggestion_is_ai = False
        if overdue_rolled_in:
            ai_suggestion = f'有 {len(overdue_rolled_in)} 个活动已过期，建议先处理最紧急的'
        elif conflict_ids:
            ai_suggestion = f'今天有 {len(conflict_ids)} 个活动时间冲突，注意调整'
        elif ongoing:
            ai_suggestion = f'当前有 {len(ongoing)} 个活动进行中，专注完成它们'

    # 六个分组互斥，合并后一次 attach_costs：
    # 原先每组各发 2 条聚合（共 12 条），现在固定 2 条
    all_activities = [*ongoing, *starting_today, *ending_today,
                      *upcoming, *recently_done, *in_progress, *overdue_rolled_in]
    _annotate_blocked(all_activities)
    attach_costs(all_activities)

    # 行为模式洞察：从已有 habit 类记忆读取（cron 每日 07:00 已算好存库），不再实时重算
    pattern_insights = []
    try:
        from memory.models import Memory
        pattern_insights = list(
            visible_qs(Memory, user)
            .filter(category='habit', content__startswith='[模式]')
            .values_list('content', flat=True)[:3]
        )
        # 剥掉 [模式] 前缀
        pattern_insights = [s.replace('[模式] ', '', 1) for s in pattern_insights]
    except Exception as exc:
        logger.warning('行为模式读取降级: %s', exc)

    return {
        'today': today,
        'ongoing': ongoing,
        'starting_today': starting_today,
        'ending_today': ending_today,
        'upcoming': upcoming,
        'recently_done': recently_done,
        'in_progress': in_progress,
        'overdue_rolled_in': overdue_rolled_in,
        'conflict_ids': conflict_ids,
        'ai_suggestion': ai_suggestion,
        'ai_suggestion_is_ai': ai_suggestion_is_ai,
        'pattern_insights': pattern_insights,
        'today_expense': today_expense,
        'this_week_expense': this_week_expense,
        'ongoing_count': len(ongoing) + len(starting_today),
        'in_progress_count': exclude_daily_bucket(qs).filter(status='in_progress').count(),
    }


def daily_brief_payload(user):
    """daily 简报卡的快照数据（chat.local_cards 注册的 gather）

    非 AI 快捷卡片机制的数据源：纯服务端查询，不碰云端 Agent、不耗 token。
    返回值整份存进 Message.payload.card_data，历史渲染只读快照不回查 ——
    所以这里只放可 JSON 序列化的标量（活动只留 id/name/meta/cost）。
    """
    data = gather_daily(user)
    today = data['today']

    def _section(key, title, items, limit=5):
        rows = []
        for a in items[:limit]:
            meta = a.get_status_display()
            if a.date_range:
                meta += f' · {a.date_range}'
            rows.append({
                'id': a.id,
                'name': a.name,
                'meta': meta,
                'cost': float(getattr(a, 'total_cost', 0) or 0),
                'blocked': bool(getattr(a, 'is_blocked', False)),
            })
        return {'key': key, 'title': title, 'items': rows, 'total': len(items)}

    sections = [
        _section('ongoing', '今日进行中', [*data['ongoing'], *data['starting_today']]),
        _section('ending_today', '今日结束', data['ending_today']),
        _section('upcoming', '即将开始（7 天内）', data['upcoming']),
        _section('recently_done', '近期完成', data['recently_done']),
        _section('in_progress', '长期进行中', data['in_progress']),
        _section('overdue', '已过期待处理', data['overdue_rolled_in']),
    ]
    return {
        'today_display': f'{today.month}月{today.day}日 · {WEEKDAY_LABELS[today.weekday()]}',
        'counts': {
            'ongoing': data['ongoing_count'],
            'in_progress': data['in_progress_count'],
            'overdue': len(data['overdue_rolled_in']),
            'conflict': len(data['conflict_ids']),
        },
        'expenses': {'today': data['today_expense'], 'week': data['this_week_expense']},
        'ai_suggestion': data['ai_suggestion'] or '',
        'insights': data['pattern_insights'],
        # 空分组直接不进快照：历史卡里留一排「无」的空壳毫无信息量
        'sections': [s for s in sections if s['total']],
    }


@login_required
def daily_view(request):
    """/daily/ 页面已下线（2026-09-19，用户定策）：daily 信息与后续操作全部收进
    对话里的 daily 简报卡（chat/partials/local_cards.html 直出 + 卡内操作行）。

    路由与 name='daily' 保留：旧书签 / 推送 / 模板 url 标签不破，重定向回首页
    （= 对话列表），零跨 app import。数据层 gather_daily 仍是卡片共用的。
    """
    return redirect('home')


@login_required
def refresh_suggestion(request):
    """手动刷新今日 AI 建议：重新调用 AI 生成并缓存，返回 JSON"""
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)

    from ..models import DailySuggestion
    from ..management.commands.generate_daily_suggestion import _generate_suggestion_for_user

    today = timezone.localdate()
    try:
        suggestion, is_ai = _generate_suggestion_for_user(request.user, today)
        DailySuggestion.objects.update_or_create(
            user=request.user, date=today,
            defaults={'suggestion': suggestion, 'is_ai': is_ai},
        )
        return JsonResponse({
            'suggestion': suggestion,
            'is_ai': is_ai,
        })
    except Exception as exc:
        logger.warning('手动刷新建议失败: %s', exc)
        return JsonResponse({'error': '刷新失败，请稍后重试'}, status=500)


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
