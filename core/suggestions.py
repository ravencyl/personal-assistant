"""Daily 页面 AI 建议引擎（纯规则，零 API 成本）"""
from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum, Count


def generate_suggestions(user):
    """生成今日建议列表，每条建议包含 text（展示文本）和 icon（可选图标标识）"""
    today = timezone.localdate()
    suggestions = []

    from activities.models import Activity, Expense

    # 规则 1：明天有活动即将开始
    tomorrow = today + timedelta(days=1)
    starting_tomorrow = Activity.objects.filter(
        user=user, start_date=tomorrow, status='planned'
    ).count()
    if starting_tomorrow > 0:
        suggestions.append({
            'text': f'明天有 {starting_tomorrow} 个活动即将开始，要不要提前准备？',
            'icon': 'calendar',
        })

    # 规则 2：本周消费 vs 上周
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    this_week_expense = Expense.objects.filter(
        user=user, paid_at__gte=week_start, paid_at__lte=today
    ).aggregate(s=Sum('amount'))['s'] or 0
    last_week_expense = Expense.objects.filter(
        user=user, paid_at__gte=last_week_start, paid_at__lt=week_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    this_week = float(this_week_expense)
    last_week = float(last_week_expense)
    if last_week > 0:
        change_pct = round((this_week - last_week) / last_week * 100, 1)
        if change_pct > 20:
            suggestions.append({
                'text': f'本周消费 ¥{this_week:.0f}，比上周多了 {change_pct:.0f}%，注意控制开支',
                'icon': 'expense',
            })
        elif change_pct < -20:
            suggestions.append({
                'text': f'本周消费 ¥{this_week:.0f}，比上周少了 {abs(change_pct):.0f}%，继续保持',
                'icon': 'expense',
            })
    elif this_week > 0:
        suggestions.append({
            'text': f'本周已消费 ¥{this_week:.0f}',
            'icon': 'expense',
        })

    # 规则 3：有活动已进行中超过 7 天
    long_running = Activity.objects.filter(
        user=user, status='in_progress',
        start_date__lte=today - timedelta(days=7),
    )
    if long_running.exists():
        names = [a.name for a in long_running[:3]]
        name_str = '、'.join(f'「{n}」' for n in names)
        suffix = f'等 {long_running.count()} 个活动' if long_running.count() > 3 else ''
        suggestions.append({
            'text': f'{name_str}{suffix}已进行中超过一周了，进度如何？',
            'icon': 'alert',
        })

    # 规则 4：有计划中的活动但没有开始日期
    no_date = Activity.objects.filter(
        user=user, status='planned', start_date__isnull=True
    ).count()
    if no_date > 0:
        suggestions.append({
            'text': f'有 {no_date} 个计划中的活动还没有开始日期，要不要安排一下？',
            'icon': 'plan',
        })

    # 规则 5：计划状态超过 30 天未变动
    stale_planned = Activity.objects.filter(
        user=user, status='planned',
        updated_at__lte=timezone.now() - timedelta(days=30),
    )
    if stale_planned.exists():
        names = [a.name for a in stale_planned[:3]]
        name_str = '、'.join(f'「{n}」' for n in names)
        suffix = f'等 {stale_planned.count()} 个活动' if stale_planned.count() > 3 else ''
        suggestions.append({
            'text': f'{name_str}{suffix}已计划超过 30 天仍未开始，是否要调整或取消？',
            'icon': 'stale',
        })

    # 规则 6：今日消费提醒
    today_expense = Expense.objects.filter(
        user=user, paid_at=today
    ).aggregate(s=Sum('amount'))['s'] or 0
    if float(today_expense) == 0:
        suggestions.append({
            'text': '今天还没有消费记录',
            'icon': 'expense',
        })

    return suggestions[:5]  # 最多返回 5 条建议
