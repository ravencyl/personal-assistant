"""Daily 页面 AI 建议引擎（纯规则，零 API 成本）

架构约定：
- 每条规则一个 `_rule_xxx(user, today)` 函数，返回单个建议 dict、
  建议 dict 列表，或 None（无建议）。
- 建议 dict 格式：{'text': str, 'icon': str, 'key': str, 'action': dict|None,
  'followup': str, 'source': 'rule'}
- action 三种 kind（无 kind 字段视为 link，向后兼容）：
  {'kind': 'link', 'label', 'url'}        页面跳转
  {'kind': 'tool', 'label', 'tool', 'params', 'confirm'}  Agent 工具直操（白名单内）
  {'kind': 'post', 'label', 'panel'}      前端打开对应浮层（如快记）
- followup：点击整条建议时发送到聊天浮窗的追问 prompt，缺省自动生成。
- `generate_suggestions(user)` 按 `_RULES` 顺序执行、聚合、过滤已关闭、
  截断至最多 6 条，返回结构供 activities.views.daily_view 注入 Daily 页。
- 结果按用户缓存 10 分钟；建议数据源模型保存/删除时通过信号清除缓存。
"""
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from core.utils import week_monday, pct_change, char_overlap_ratio

SUGGESTION_CACHE_TIMEOUT = 600  # 10 分钟
MAX_SUGGESTIONS = 6  # 最多返回 6 条建议（规则 5 + AI 洞察 1-2）

# 建议动作可调用的 Agent 工具白名单：{工具名: 是否需要确认弹窗}
# 新增工具前评估误操作风险；规则引擎与 AI 洞察共用这份白名单，
# 端点校验见 core/suggestion_views.py。
SUGGESTION_TOOLS = {
    'activities.set_status': False,
    'activities.batch_status': True,
    'activities.move_date': True,
    'reminders.complete': False,
}


def _tool_action(label, tool, params, confirm=None, summary=''):
    """构造可直接执行 Agent 工具的建议动作（工具名必须已在 SUGGESTION_TOOLS）"""
    assert tool in SUGGESTION_TOOLS, f'工具 {tool} 不在建议白名单'
    if confirm is None:
        confirm = SUGGESTION_TOOLS[tool]
    return {'kind': 'tool', 'label': label, 'tool': tool,
            'params': params, 'confirm': confirm, 'summary': summary}


def _cache_key(user_id):
    return f'suggestions_{user_id}'


def _states_cache_key(user_id):
    return f'suggestion_states_{user_id}'


def _normalize(suggestion, rule_name, today):
    """确保建议包含所有必需字段"""
    if 'key' not in suggestion:
        suggestion['key'] = f'{rule_name}:{today.isoformat()}'
    if 'action' not in suggestion:
        suggestion['action'] = None
    if 'source' not in suggestion:
        suggestion['source'] = 'rule'
    if 'followup' not in suggestion or not suggestion.get('followup'):
        text = (suggestion.get('text') or '').strip()
        suggestion['followup'] = f'关于这条建议：“{text[:40]}”，帮我具体分析并给出下一步'
    return suggestion


def compute_habit_streaks(user, today):
    """计算用户所有活跃 daily 习惯的连续打卡天数（截至昨日）

    返回 [{'recurring': RecurringActivity, 'name': str, 'streak': int}, ...]
    按 streak 降序。供规则引擎与 AI 洞察命令共用。
    """
    from activities.models import Activity

    recurring_sources = list(
        user.recurring_activities.filter(frequency='daily', is_active=True)
    )
    if not recurring_sources:
        return []

    results = []
    for rec in recurring_sources:
        instances = dict(
            Activity.objects.filter(
                user=user,
                recurring_source=rec,
                start_date__gte=today - timedelta(days=60),
                start_date__lt=today,
            ).values_list('start_date', 'status')
        )
        streak = 0
        day = today - timedelta(days=1)
        while day in instances and instances[day] == 'done':
            streak += 1
            day -= timedelta(days=1)
        if streak > 0:
            results.append({'recurring': rec, 'name': rec.name, 'streak': streak})

    results.sort(key=lambda x: -x['streak'])
    return results


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
            'key': f'starting_tomorrow:{tomorrow.isoformat()}',
            'action': {'kind': 'link', 'label': '查看',
                       # 深链参数必须是列表页真正支持的筛选字段（date_from/date_to），
                       # 用 start_date 会被 filter_activities 静默忽略，导致点开是全量列表
                       'url': reverse('activities:activity_list')
                              + f'?status=planned&date_from={tomorrow.isoformat()}&date_to={tomorrow.isoformat()}'},
            'followup': f'明天有 {count} 个活动即将开始，帮我梳理一下需要提前准备什么',
        }
    return None


def _rule_weekly_expense(user, today):
    """规则 2：本周消费对比上周"""
    from activities.models import Expense

    week_start = week_monday(today)
    last_week_start = week_start - timedelta(days=7)
    this_week_expense = Expense.objects.filter(
        user=user, paid_at__gte=week_start, paid_at__lte=today
    ).aggregate(s=Sum('amount'))['s'] or 0
    last_week_expense = Expense.objects.filter(
        user=user, paid_at__gte=last_week_start, paid_at__lt=week_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    this_week = float(this_week_expense)
    last_week = float(last_week_expense)
    report_url = reverse('activities:expense_report')
    if last_week > 0:
        change_pct = pct_change(this_week, last_week)
        if change_pct > 20:
            return {
                'text': f'本周消费 ¥{this_week:.0f}，比上周多了 {change_pct:.0f}%，注意控制开支',
                'icon': 'expense',
                'key': f'weekly_expense:{week_start.isoformat()}',
                'action': {'kind': 'link', 'label': '消费报告', 'url': report_url},
                'followup': f'本周消费 ¥{this_week:.0f} 比上周多了 {change_pct:.0f}%，帮我分析变化原因和可压缩的类别',
            }
        if change_pct < -20:
            return {
                'text': f'本周消费 ¥{this_week:.0f}，比上周少了 {abs(change_pct):.0f}%，继续保持',
                'icon': 'expense',
                'key': f'weekly_expense:{week_start.isoformat()}',
                'action': {'kind': 'link', 'label': '消费报告', 'url': report_url},
                'followup': f'本周消费比上周少了 {abs(change_pct):.0f}%，帮我看看哪些类别降下来了',
            }
        return None
    if this_week > 0:
        return {
            'text': f'本周已消费 ¥{this_week:.0f}',
            'icon': 'expense',
            'key': f'weekly_expense:{week_start.isoformat()}',
            'action': {'kind': 'link', 'label': '消费报告', 'url': report_url},
            'followup': f'本周已消费 ¥{this_week:.0f}，帮我看看各类别占比是否健康',
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
        'key': f'long_running:{today.isoformat()}',
        'action': {'kind': 'link', 'label': '查看',
                   'url': reverse('activities:activity_list') + '?status=in_progress'},
        'followup': f'{name_str}{suffix}已经进行中超过一周，帮我梳理进度并建议下一步',
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
            'key': f'no_start_date:{today.isoformat()}',
            'action': {'kind': 'link', 'label': '去安排',
                       'url': reverse('activities:activity_list') + '?status=planned'},
            'followup': f'我有 {no_date} 个计划中的活动还没定开始日期，帮我根据优先级建议一个排期方案',
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
    ids = list(stale_planned.values_list('id', flat=True)[:10])
    names = [a.name for a in stale_planned[:3]]
    name_str = '、'.join(f'「{n}」' for n in names)
    suffix = f'等 {stale_planned.count()} 个活动' if stale_planned.count() > 3 else ''
    return {
        'text': f'{name_str}{suffix}已计划超过 30 天仍未开始，是否要调整或取消？',
        'icon': 'stale',
        'key': f'stale_planned:{today.isoformat()}',
        'action': _tool_action(
            '全部取消', 'activities.batch_status',
            {'status': 'cancelled', 'target_ids': ids},
            summary=f'将 {len(ids)} 个长期未开始的活动（{name_str}{suffix}）标记为已取消'),
        'followup': f'{name_str}{suffix}计划超过 30 天未开始，帮我判断该推进、改期还是取消',
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
            'key': f'today_expense:{today.isoformat()}',
            'action': {'kind': 'post', 'label': '记一笔', 'panel': 'expense'},
            'followup': '今天还没有记消费，帮我回顾一下今天可能有哪些开支需要补记',
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
            'key': f'budget:{a.id}',
            'action': {'kind': 'link', 'label': '查看', 'url': reverse('activities:activity_detail', args=[a.id])},
            'followup': f'「{a.name}」{label}（¥{spent:.0f}/¥{a.budget:.0f}），帮我分析剩余预算怎么分配',
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
            'key': f'reminder:{r.id}',
            'action': _tool_action('知道了', 'reminders.complete', {'reminder_id': r.id}),
            'followup': f'提醒“{r.content}”快到期了，帮我想想该怎么处理',
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
            timezone.datetime.combine(week_monday(today), timezone.datetime.min.time())
        ),
    ).exists()
    if not has_report:
        week_start = week_monday(today)
        return {
            'text': '本周报告已准备好，要看看吗？',
            'icon': 'plan',
            'key': f'weekly_report:{week_start.isoformat()}',
            'action': {'kind': 'link', 'label': '查看周报', 'url': reverse('weekly_report')},
            'followup': '帮我生成本周周报，并总结本周的关键进展',
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
            'key': f'habit_missed:{a.id}',
            'action': _tool_action('补打卡', 'activities.set_status',
                                   {'activity_id': a.id, 'status': 'done'}),
            'followup': f'「{a.name}」昨天断签了，帮我想想怎么调整节奏把习惯恢复',
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
            'key': f'ending:{a.id}',
            'action': _tool_action(
                '标记完成', 'activities.set_status', {'activity_id': a.id, 'status': 'done'},
                summary=f'将「{a.name}」标记为已完成'),
            'followup': f'「{a.name}」即将到期还没完成，帮我拆解剩下的工作',
        }
        for a in soon[:2]
    ] or None




def _rule_goal_progress(user, today):
    """规则 12：目标进度跟踪——Memory 中的目标与活动关联"""
    from memory.models import Memory
    from activities.models import Activity

    goals = Memory.objects.filter(
        user=user, category='goal'
    ).order_by('-importance')[:3]
    if not goals.exists():
        return None

    active_activities = list(
        Activity.objects.filter(user=user, status__in=('planned', 'in_progress'))
        .values_list('name', 'id', 'status')
    )
    if not active_activities:
        return None

    suggestions = []
    for goal in goals:
        goal_text = goal.content.lower()
        matched = None
        for name, act_id, status in active_activities:
            # 字符重叠率匹配（共用实现，memory 查重用的是单向 contains 口径）
            if char_overlap_ratio(goal_text, name.lower()) > 0.4:
                matched = (name, act_id, status)
                break
        if matched:
            name, act_id, status = matched
            if status == 'in_progress':
                suggestions.append({
                    'text': f'目标「{goal.content}」相关的「{name}」进行中，加油',
                    'icon': 'goal',
                    'key': f'goal:{goal.id}',
                    'action': {'kind': 'link', 'label': '查看', 'url': reverse('activities:activity_detail', args=[act_id])},
                    'followup': f'目标「{goal.content}」相关的「{name}」正在进行，帮我看看下一步可以推进什么',
                })
            else:
                suggestions.append({
                    'text': f'目标「{goal.content}」相关的「{name}」已规划，准备开始吧',
                    'icon': 'goal',
                    'key': f'goal:{goal.id}',
                    'action': _tool_action(
                        '开始活动', 'activities.set_status', {'activity_id': act_id, 'status': 'in_progress'},
                        summary=f'将「{name}」标记为进行中'),
                    'followup': f'目标「{goal.content}」相关的「{name}」还没开始，帮我规划启动步骤',
                })
        else:
            suggestions.append({
                'text': f'目标「{goal.content}」近期没有相关进展，要不要规划下一步？',
                'icon': 'goal',
                'key': f'goal:{goal.id}',
                'action': {'kind': 'link', 'label': '创建活动', 'url': reverse('activities:activity_create')},
                'followup': f'围绕目标「{goal.content}」，帮我拆解成几个可执行的具体任务',
            })
        if len(suggestions) >= 2:
            break
    return suggestions or None


def _rule_time_investment(user, today):
    """规则 13：时间投入分析——本周/上周完成活动的预估耗时环比"""
    from activities.models import Activity

    week_start = week_monday(today)
    last_week_start = week_start - timedelta(days=7)

    this_week = Activity.objects.filter(
        user=user, status='done',
        end_date__gte=week_start, end_date__lte=today,
        duration_minutes__isnull=False,
    ).aggregate(total=Sum('duration_minutes'))['total'] or 0

    last_week = Activity.objects.filter(
        user=user, status='done',
        end_date__gte=last_week_start, end_date__lt=week_start,
        duration_minutes__isnull=False,
    ).aggregate(total=Sum('duration_minutes'))['total'] or 0

    if last_week <= 0 or this_week <= 0:
        return None

    change_pct = pct_change(this_week, last_week)
    if abs(change_pct) < 30:
        return None

    hours_this = this_week / 60
    if change_pct > 30:
        text = f'本周投入约 {hours_this:.0f} 小时，比上周多了 {change_pct:.0f}%，注意休息'
    else:
        text = f'本周投入约 {hours_this:.0f} 小时，比上周少了 {abs(change_pct):.0f}%'
    return {
        'text': text,
        'icon': 'plan',
        'key': f'time_invest:{week_start.isoformat()}',
        'action': {'kind': 'link', 'label': '本周活动',
                   'url': reverse('activities:activity_list') + '?status=done'},
        'followup': text + '，帮我分析时间投入的变化是否合理',
    }


def _rule_expense_anomaly(user, today):
    """规则 14：消费异常检测——单日消费显著高于近期日均"""
    from activities.models import Expense

    today_expense = float(
        Expense.objects.filter(user=user, paid_at=today)
        .aggregate(s=Sum('amount'))['s'] or 0
    )
    if today_expense <= 0:
        return None

    # 近 30 天有消费日的日均
    from django.db.models import Count
    past_30 = Expense.objects.filter(
        user=user,
        paid_at__gte=today - timedelta(days=30),
        paid_at__lt=today,
    )
    daily_totals = past_30.values('paid_at').annotate(day_total=Sum('amount'))
    day_count = daily_totals.count()
    if day_count == 0:
        return None
    avg_daily = float(sum(d['day_total'] for d in daily_totals) / day_count)

    threshold = max(avg_daily * 3, 50)  # 至少 50 元门槛
    if today_expense > threshold:
        return {
            'text': f'今日消费 ¥{today_expense:.0f}，明显高于近期日均（¥{avg_daily:.0f}），留意一下',
            'icon': 'expense',
            'key': f'expense_anomaly:{today.isoformat()}',
            'action': {'kind': 'link', 'label': '消费报告', 'url': reverse('activities:expense_report')},
            'followup': f'今日消费 ¥{today_expense:.0f} 明显高于日均，帮我复盘这笔开支是否必要',
        }
    return None


def _rule_habit_streak(user, today):
    """规则 15：习惯连续打卡正向激励（≥3 天）"""
    streaks = compute_habit_streaks(user, today)
    suggestions = []
    for item in streaks:
        if item['streak'] >= 3:
            suggestions.append({
                'text': f'「{item["name"]}」已连续打卡 {item["streak"]} 天，继续保持',
                'icon': 'habit',
                'key': f'habit_streak:{item["recurring"].id}:{item["streak"]}',
                'action': {'kind': 'link', 'label': '习惯列表', 'url': reverse('activities:recurring_list')},
                'followup': f'「{item["name"]}」已连续打卡 {item["streak"]} 天，帮我看看怎么让它更容易坚持',
            })
        if len(suggestions) >= 2:
            break
    return suggestions or None


def _rule_subtask_progress(user, today):
    """规则 16：子任务接近完成——完成 ≥80% 时鼓励一鼓作气"""
    from django.db.models import Count, Q
    from activities.models import Activity

    progress = (
        Activity.objects.filter(user=user, parent__isnull=False)
        .values('parent_id', 'parent__name')
        .annotate(
            done_count=Count('id', filter=Q(status='done')),
            total_count=Count('id'),
        )
        .filter(total_count__gte=2)
    )

    suggestions = []
    for row in progress:
        if row['total_count'] == 0:
            continue
        ratio = row['done_count'] / row['total_count']
        remaining = row['total_count'] - row['done_count']
        if ratio >= 0.8 and remaining > 0:
            undone_ids = list(
                Activity.objects.filter(
                    parent_id=row['parent_id'], user=user
                ).exclude(status='done').values_list('id', flat=True)[:20]
            )
            suggestions.append({
                'text': f'「{row["parent__name"]}」只剩 {remaining} 个子任务，一鼓作气完成吧',
                'icon': 'plan',
                'key': f'subtask:{row["parent_id"]}',
                'action': _tool_action(
                    '完成子任务', 'activities.batch_status',
                    {'status': 'done', 'target_ids': undone_ids},
                    summary=f'将「{row["parent__name"]}」剩余 {remaining} 个子任务标记为已完成'),
                'followup': f'「{row["parent__name"]}」只剩 {remaining} 个子任务，帮我看看收尾需要注意什么',
            })
        if len(suggestions) >= 2:
            break
    return suggestions or None


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
    _rule_goal_progress,
    _rule_time_investment,
    _rule_expense_anomaly,
    _rule_habit_streak,
    _rule_subtask_progress,
]


def generate_suggestions(user):
    """生成今日建议列表，每条建议包含 text/icon/key/action/source

    AI 洞察（DailyInsight）置顶，规则建议跟随，总上限 6 条。
    结果按用户缓存 10 分钟；数据源模型变更时经信号失效，
    最坏情况下依赖 TTL 过期。
    """
    key = _cache_key(user.id)
    cached = cache.get(key)
    if cached is not None:
        return cached

    today = timezone.localdate()

    # 获取已关闭/已读指纹集合
    dismissed, read_states = _get_suggestion_states(user)

    # AI 洞察置顶（来自 DailyInsight，cron 预生成）
    suggestions = []
    ai_insights = _get_daily_insights(user, today)
    for item in ai_insights:
        if item.get('key') not in dismissed:
            item['is_read'] = item.get('key') in read_states
            suggestions.append(item)

    # 规则建议跟随
    for rule in _RULES:
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
        result = rule(user, today)
        if not result:
            continue
        if isinstance(result, dict):
            result = [result]
        for s in result:
            if len(suggestions) >= MAX_SUGGESTIONS:
                break
            s = _normalize(s, rule.__name__.replace('_rule_', ''), today)
            if s['key'] not in dismissed:
                s['is_read'] = s['key'] in read_states
                suggestions.append(s)

    cache.set(key, suggestions, SUGGESTION_CACHE_TIMEOUT)
    return suggestions


def _get_daily_insights(user, today):
    """获取当日 AI 洞察（已生成的），返回格式化后的建议列表"""
    from core.models import DailyInsight

    insight_obj = DailyInsight.objects.filter(
        user=user, insight_date=today, status__in=('ready', 'fallback')
    ).first()
    if not insight_obj or not insight_obj.insights:
        return []

    results = []
    for item in insight_obj.insights:
        if not isinstance(item, dict) or not item.get('text'):
            continue
        text = item['text']
        results.append({
            'text': text,
            'icon': item.get('icon', 'plan'),
            'key': item.get('key', f'ai:{today.isoformat()}:{len(results)}'),
            'action': item.get('action'),
            'followup': item.get('followup') or f'关于这条洞察：“{text[:40]}”，帮我具体分析并给出下一步',
            'source': 'ai',
        })
    return results


def _get_suggestion_states(user):
    """获取用户建议交互状态（已关闭指纹集合 + 已读指纹集合，带缓存）"""
    from core.models import SuggestionState

    skey = _states_cache_key(user.id)
    cached = cache.get(skey)
    if cached is not None:
        return cached

    dismissed = set()
    read = set()
    for fp, action in SuggestionState.objects.filter(user=user).values_list('fingerprint', 'action'):
        if action == 'dismissed':
            dismissed.add(fp)
        else:
            read.add(fp)
    result = (dismissed, read)
    cache.set(skey, result, SUGGESTION_CACHE_TIMEOUT)
    return result


def invalidate_user_caches(user_id):
    """清除该用户的建议与建议交互状态缓存

    视图 / 信号 / 定时命令三处统一走这里，
    避免只清 suggestions_ 而漏掉 suggestion_states_ 导致两键不同步。
    """
    cache.delete(_cache_key(user_id))
    cache.delete(_states_cache_key(user_id))


def invalidate_suggestions_cache(sender, instance, **kwargs):
    """信号处理器：建议数据源模型保存/删除时清除该用户的建议缓存"""
    user_id = getattr(instance, 'user_id', None)
    if user_id:
        invalidate_user_caches(user_id)


def connect_invalidation_signals():
    """挂载建议缓存失效信号（由 CoreConfig.ready 调用，不得在 import 期执行）"""
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


# ────────────────────────────────────────────────
# 每日规划（打卡与提醒）——独立于建议规则引擎，纯规则、不缓存，
# 由 daily_view 每次请求调用一次，避免重复查询。
# ────────────────────────────────────────────────

def generate_daily_plan(user):
    """生成 Daily 页「打卡与提醒」结构化数据（纯规则，零 AI）

    只放下面活动卡片区覆盖不到的三类信息；今日发生的活动本身不在此列出，
    避免与 daily_view 的「今日进行中」卡片重复（同一活动上下各出现一次）。

    返回 dict：
    - habits:         循环活动今日实例列表（打卡状态）
    - subtask_groups: 未完成子活动 Top 5，按父活动分组
    - reminders:      待触发提醒列表
    - is_empty:       三组全部为空
    """
    from activities.models import Activity
    from core.models import Reminder

    today = timezone.localdate()

    habits = list(Activity.objects.filter(
        user=user,
        recurring_source__isnull=False,
        start_date=today,
        recurring_source__is_active=True,
    ).select_related('recurring_source').order_by('id')[:10])

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

    day_end = timezone.make_aware(
        timezone.datetime.combine(today + timedelta(days=1), timezone.datetime.min.time())
    )
    reminders = list(Reminder.objects.filter(
        user=user, status='pending', trigger_at__lt=day_end,
    ).order_by('trigger_at')[:5])

    return {
        'habits': habits,
        'subtask_groups': subtask_groups,
        'reminders': reminders,
        'is_empty': not (habits or subtask_groups or reminders),
    }
