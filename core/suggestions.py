"""Daily 页面 AI 建议引擎（纯规则，零 API 成本）

架构约定：
- 每条规则一个 `_rule_xxx(user, today)` 函数，返回单个建议 dict（{text, icon}）、
  建议 dict 列表，或 None（无建议）。
- `generate_suggestions(user)` 按 `_RULES` 顺序执行、聚合、截断至最多 5 条，
  返回结构与调用方契约不变（供 activities.views.daily_view 注入 Daily 页）。
- 结果按用户缓存 10 分钟；建议数据源模型（Activity/Expense/RecurringActivity/
  Reminder/Article）保存/删除时通过信号清除对应用户缓存。
"""
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

SUGGESTION_CACHE_TIMEOUT = 600  # 10 分钟
MAX_SUGGESTIONS = 5  # 最多返回 5 条建议


def _cache_key(user_id):
    return f'suggestions_{user_id}'


# ────────────────────────────────────────────────
# 规则函数（顺序即建议优先级，总数超限时靠后规则被截断）
# ────────────────────────────────────────────────

def _rule_starting_tomorrow(user, today):
    """规则 1：明天有活动即将开始"""
    from activities.models import Activity

    tomorrow = today + timedelta(days=1)
    count = Activity.objects.filter(
        user=user, start_date=tomorrow, status='planned'
    ).count()
    if count > 0:
        return {
            'text': f'明天有 {count} 个活动即将开始，要不要提前准备？',
            'icon': 'calendar',
        }
    return None


def _rule_weekly_expense(user, today):
    """规则 2：本周消费对比上周"""
    from activities.models import Expense

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
            return {
                'text': f'本周消费 ¥{this_week:.0f}，比上周多了 {change_pct:.0f}%，注意控制开支',
                'icon': 'expense',
            }
        if change_pct < -20:
            return {
                'text': f'本周消费 ¥{this_week:.0f}，比上周少了 {abs(change_pct):.0f}%，继续保持',
                'icon': 'expense',
            }
        return None
    if this_week > 0:
        return {
            'text': f'本周已消费 ¥{this_week:.0f}',
            'icon': 'expense',
        }
    return None


def _rule_long_running(user, today):
    """规则 3：有活动已进行中超过 7 天"""
    from activities.models import Activity

    long_running = Activity.objects.filter(
        user=user, status='in_progress',
        start_date__lte=today - timedelta(days=7),
    )
    if not long_running.exists():
        return None
    names = [a.name for a in long_running[:3]]
    name_str = '、'.join(f'「{n}」' for n in names)
    suffix = f'等 {long_running.count()} 个活动' if long_running.count() > 3 else ''
    return {
        'text': f'{name_str}{suffix}已进行中超过一周了，进度如何？',
        'icon': 'alert',
    }


def _rule_no_start_date(user, today):
    """规则 4：有计划中的活动但没有开始日期"""
    from activities.models import Activity

    no_date = Activity.objects.filter(
        user=user, status='planned', start_date__isnull=True
    ).count()
    if no_date > 0:
        return {
            'text': f'有 {no_date} 个计划中的活动还没有开始日期，要不要安排一下？',
            'icon': 'plan',
        }
    return None


def _rule_stale_planned(user, today):
    """规则 5：计划状态超过 30 天未变动"""
    from activities.models import Activity

    stale_planned = Activity.objects.filter(
        user=user, status='planned',
        updated_at__lte=timezone.now() - timedelta(days=30),
    )
    if not stale_planned.exists():
        return None
    names = [a.name for a in stale_planned[:3]]
    name_str = '、'.join(f'「{n}」' for n in names)
    suffix = f'等 {stale_planned.count()} 个活动' if stale_planned.count() > 3 else ''
    return {
        'text': f'{name_str}{suffix}已计划超过 30 天仍未开始，是否要调整或取消？',
        'icon': 'stale',
    }


def _rule_today_expense(user, today):
    """规则 6：今日消费提醒"""
    from activities.models import Expense

    today_expense = Expense.objects.filter(
        user=user, paid_at=today
    ).aggregate(s=Sum('amount'))['s'] or 0
    if float(today_expense) == 0:
        return {
            'text': '今天还没有消费记录',
            'icon': 'expense',
        }
    return None


def _rule_budget_warning(user, today):
    """规则 7：预算预警（批量聚合取费用合计，避免逐活动 N+1 查询）"""
    from activities.models import Activity, Expense

    acts = list(
        Activity.objects.filter(user=user, budget__isnull=False).exclude(budget__lte=0)
    )
    if not acts:
        return None

    # 一次聚合查询取回所有活动的费用合计（模式同 activities.views.attach_costs）
    totals = dict(
        Expense.objects.filter(activity_id__in=[a.id for a in acts])
        .values('activity_id').annotate(total=Sum('amount'))
        .values_list('activity_id', 'total')
    )

    suggestions = []
    for a in acts:
        spent = float(totals.get(a.id, 0) or 0)
        budget = float(a.budget)
        ratio = spent / budget
        if ratio >= 1.0:
            label = '已超预算'
        elif ratio >= 0.8:
            label = '接近预算'
        else:
            continue
        suggestions.append({
            'text': f'「{a.name}」{label}（已花费 ¥{spent:.0f} / 预算 ¥{a.budget:.0f}）',
            'icon': 'expense',
        })
        if len(suggestions) >= 2:
            break
    return suggestions or None


def _rule_upcoming_reminders(user, today):
    """规则 8：有即将到期的提醒"""
    from core.models import Reminder

    upcoming_reminders = Reminder.objects.filter(
        user=user, status='pending',
        trigger_at__lte=timezone.now() + timedelta(hours=2),
        trigger_at__gte=timezone.now() - timedelta(hours=1),
    )
    return [
        {
            'text': f'提醒：{r.content}（即将到期）',
            'icon': 'alert',
        }
        for r in upcoming_reminders[:2]
    ] or None


def _rule_weekly_report(user, today):
    """规则 9：每周五提示生成周报"""
    if today.weekday() != 4:  # 周五
        return None
    from knowledge.models import Article

    has_report = Article.objects.filter(
        user=user,
        tags__name='report-weekly',
        created_at__gte=timezone.make_aware(
            timezone.datetime.combine(today - timedelta(days=today.weekday()), timezone.datetime.min.time())
        ),
    ).exists()
    if not has_report:
        return {
            'text': '本周报告已准备好，要看看吗？',
            'icon': 'plan',
        }
    return None


def _rule_habit_missed(user, today):
    """规则 10：习惯断签——daily 循环活动昨日生成的实例未完成打卡"""
    from activities.models import Activity

    yesterday = today - timedelta(days=1)
    missed = Activity.objects.filter(
        user=user,
        start_date=yesterday,
        recurring_source__isnull=False,
        recurring_source__frequency='daily',
    ).exclude(status='done')
    return [
        {
            'text': f'「{a.name}」昨天没有打卡',
            'icon': 'alert',
        }
        for a in missed[:2]
    ] or None


def _rule_ending_soon(user, today):
    """规则 11：临期活动——end_date 距今 ≤3 天且未完结/取消"""
    from activities.models import Activity

    soon = Activity.objects.filter(
        user=user,
        end_date__gte=today,
        end_date__lte=today + timedelta(days=3),
    ).exclude(status__in=('done', 'cancelled')).order_by('end_date')
    return [
        {
            'text': f'「{a.name}」今天到期' if (a.end_date - today).days == 0
            else f'「{a.name}」还有 {(a.end_date - today).days} 天到期',
            'icon': 'calendar',
        }
        for a in soon[:2]
    ] or None


_RULES = [
    _rule_starting_tomorrow,
    _rule_weekly_expense,
    _rule_long_running,
    _rule_no_start_date,
    _rule_stale_planned,
    _rule_today_expense,
    _rule_budget_warning,
    _rule_upcoming_reminders,
    _rule_weekly_report,
    _rule_habit_missed,
    _rule_ending_soon,
]


def generate_suggestions(user):
    """生成今日建议列表，每条建议包含 text（展示文本）和 icon（可选图标标识）

    结果按用户缓存 10 分钟；数据源模型变更时经信号失效（见模块底部），
    最坏情况下依赖 TTL 过期。
    """
    key = _cache_key(user.id)
    cached = cache.get(key)
    if cached is not None:
        return cached

    today = timezone.localdate()
    suggestions = []
    for rule in _RULES:
        result = rule(user, today)
        if not result:
            continue
        if isinstance(result, dict):
            result = [result]
        suggestions.extend(result)

    suggestions = suggestions[:MAX_SUGGESTIONS]
    cache.set(key, suggestions, SUGGESTION_CACHE_TIMEOUT)
    return suggestions


# ────────────────────────────────────────────────
# 缓存失效信号
# ────────────────────────────────────────────────

def invalidate_suggestions_cache(sender, instance, **kwargs):
    """信号处理器：建议数据源模型保存/删除时清除该用户的建议缓存"""
    user_id = getattr(instance, 'user_id', None)
    if user_id:
        cache.delete(_cache_key(user_id))


def _connect_invalidation_signals():
    """挂载缓存失效信号。

    本模块由视图惰性导入，导入时 app registry 已就绪即可挂载；
    若尚未就绪则跳过，缓存依赖 10 分钟 TTL 过期兜底。
    """
    from django.apps import apps
    if not apps.ready:
        return
    from django.db.models.signals import post_save, post_delete
    from activities.models import Activity, Expense, RecurringActivity
    from core.models import Reminder
    from knowledge.models import Article

    for model in (Activity, Expense, RecurringActivity, Reminder, Article):
        post_save.connect(
            invalidate_suggestions_cache, sender=model,
            dispatch_uid=f'suggestions_invalidate_save_{model.__name__}',
        )
        post_delete.connect(
            invalidate_suggestions_cache, sender=model,
            dispatch_uid=f'suggestions_invalidate_delete_{model.__name__}',
        )


_connect_invalidation_signals()


# ────────────────────────────────────────────────
# 每日规划（今日安排）——独立于建议规则引擎，纯规则、不缓存，
# 由 daily_view 每次请求调用一次，避免重复查询。
# ────────────────────────────────────────────────

def generate_daily_plan(user):
    """生成 Daily 页「今日安排」结构化数据（纯规则，零 AI）

    返回 dict：
    - due_today:    待办活动列表——今日落在 [start_date, end_date] 区间，
                    或 start_date/end_date 为今日；排除已完成/已取消、
                    「日常开支」归属桶及循环实例（循环实例归入 habits，避免重复）
    - habits:       循环活动今日实例列表（与 daily_view 的 today_instances 同口径）
    - subtask_groups: 未完成子活动 Top 5，按父活动分组，
                    结构 [{'parent': Activity, 'children': [Activity, ...]}]
    - reminders:    待触发提醒列表——status='pending' 且 trigger_at 在今日内，
                    按触发时间升序（字段语义同 core.models.check_due_reminders）
    - is_empty:     四组全部为空（供模板渲染早间空态文案）
    """
    from django.db.models import Q
    from activities.models import Activity
    from activities.utils import exclude_daily_bucket
    from core.models import Reminder

    today = timezone.localdate()

    # ── 待办：今日区间/起止日为今日的非完成活动 ──
    due_today = list(
        exclude_daily_bucket(Activity.objects.filter(user=user)).filter(
            Q(start_date__lte=today, end_date__gte=today)
            | Q(start_date=today)
            | Q(end_date=today)
        ).filter(
            recurring_source__isnull=True,
        ).exclude(status__in=('done', 'cancelled')).order_by('start_date')[:10]
    )

    # ── 习惯：今日循环实例（照 daily_view 的 today_instances 查询口径） ──
    habits = list(Activity.objects.filter(
        user=user,
        recurring_source__isnull=False,
        start_date=today,
        recurring_source__is_active=True,
    ).select_related('recurring_source').order_by('id')[:10])

    # ── 子活动：未完成（计划/进行中）子活动取前 5，按父活动分组 ──
    children = Activity.objects.filter(
        user=user,
        parent__isnull=False,
        status__in=('planned', 'in_progress'),
    ).select_related('parent').order_by('parent_id', 'start_date', 'id')[:5]
    subtask_groups = []
    for child in children:
        if subtask_groups and subtask_groups[-1]['parent'].id == child.parent_id:
            subtask_groups[-1]['children'].append(child)
        else:
            subtask_groups.append({'parent': child.parent, 'children': [child]})

    # ── 提醒：待触发且今日到期的（到期即被 check_due_reminders 转为 fired） ──
    day_end = timezone.make_aware(
        timezone.datetime.combine(today + timedelta(days=1), timezone.datetime.min.time())
    )
    reminders = list(Reminder.objects.filter(
        user=user, status='pending', trigger_at__lt=day_end,
    ).order_by('trigger_at')[:5])

    return {
        'due_today': due_today,
        'habits': habits,
        'subtask_groups': subtask_groups,
        'reminders': reminders,
        'is_empty': not (due_today or habits or subtask_groups or reminders),
    }
