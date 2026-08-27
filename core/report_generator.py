"""智能周报/月报/年报生成服务

规则聚合统计数据 + AI 生成分析文本，保存为知识库文章。
"""
import json
import logging
from datetime import date, timedelta

from django.db.models import Sum, Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone

logger = logging.getLogger(__name__)

TYPE_LABELS = {'weekly': '周报', 'monthly': '月报', 'yearly': '年报'}


def _normalize_report_type(report_type, period_start, period_end):
    """report_type 缺省/非法时按区间长度自动适配"""
    if report_type in ('weekly', 'monthly', 'yearly'):
        return report_type
    days = (period_end - period_start).days + 1
    if days > 92:
        return 'yearly'
    if days > 14:
        return 'monthly'
    return 'weekly'


def collect_report_data(user, report_type, period_start, period_end):
    """聚合指定时间段的统计数据。

    report_type: 'weekly' / 'monthly' / 'yearly'（其他值按区间长度自动适配）
    返回 dict 包含：
    - total_activities, completed, in_progress, planned, cancelled
    - total_expense, expense_by_category
    - daily_expense (list of {date, amount}，年报为月度聚合 monthly_expense)
    - top_activities (费用最高的活动)
    - top_tags (最常用的标签)
    - prev_period_expense (上一周期费用，用于环比)
    年报额外里程碑字段：
    - checkin_days (循环活动实例完成打卡天数)
    - top_category (花费最高的费用类别)
    - most_active_month (活动最多的月份，'YYYY-MM' 或 None)
    - monthly_expense (每月费用聚合，避免逐日 N+1)
    """
    from activities.models import Activity, Expense

    report_type = _normalize_report_type(report_type, period_start, period_end)
    is_yearly = report_type == 'yearly'

    # 活动统计
    activities = Activity.objects.filter(
        user=user,
        start_date__gte=period_start,
        start_date__lte=period_end,
    )
    total = activities.count()
    status_dist = dict(
        activities.values_list('status').annotate(n=Count('id')).values_list('status', 'n')
    )

    # 费用统计
    expenses = Expense.objects.filter(
        user=user,
        paid_at__gte=period_start,
        paid_at__lte=period_end,
    )
    total_expense = float(expenses.aggregate(s=Sum('amount'))['s'] or 0)

    # 按类别统计
    expense_by_cat = dict(
        expenses.values('category').annotate(s=Sum('amount'))
        .values_list('category', 's')
    )
    expense_by_cat = {k: float(v) for k, v in expense_by_cat.items()}

    # 费用趋势：周报/月报逐日；年报按月聚合（单次查询，避免逐日 N+1）
    daily_expense = []
    monthly_expense = []
    if is_yearly:
        month_amounts = dict(
            expenses.filter(paid_at__isnull=False)
            .annotate(m=TruncMonth('paid_at'))
            .values_list('m').annotate(s=Sum('amount'))
            .values_list('m', 's')
        )
        cursor = period_start.replace(day=1)
        end_month = period_end.replace(day=1)
        while cursor <= end_month:
            monthly_expense.append({
                'month': cursor.strftime('%Y-%m'),
                'amount': float(month_amounts.get(cursor, 0) or 0),
            })
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        current = period_start
        while current <= period_end:
            day_total = float(
                expenses.filter(paid_at=current).aggregate(s=Sum('amount'))['s'] or 0
            )
            daily_expense.append({'date': current.isoformat(), 'amount': day_total})
            current += timedelta(days=1)

    # 费用最高的活动 Top 3
    top_activities = []
    for a in activities.annotate(expense_total=Sum('expenses__amount')).order_by('-expense_total')[:3]:
        if a.expense_total:
            top_activities.append({
                'name': a.name,
                'amount': float(a.expense_total),
                'status': a.status,
            })

    # 最常用标签 Top 5
    from taggit.models import Tag, TaggedItem
    from django.contrib.contenttypes.models import ContentType
    ct = ContentType.objects.get_for_model(Activity)
    activity_ids = activities.values_list('id', flat=True)
    top_tags = list(
        TaggedItem.objects.filter(
            content_type=ct,
            object_id__in=activity_ids,
        ).values('tag__name').annotate(
            n=Count('id')
        ).order_by('-n').values_list('tag__name', 'n')[:5]
    )

    # 上一周期费用（环比）：年报对比上一自然年，其余按等长前置区间
    if is_yearly:
        prev_start = date(period_start.year - 1, 1, 1)
        prev_end = date(period_start.year - 1, 12, 31)
    else:
        period_length = (period_end - period_start).days + 1
        prev_start = period_start - timedelta(days=period_length)
        prev_end = period_start - timedelta(days=1)
    prev_expense = float(
        Expense.objects.filter(
            user=user, paid_at__gte=prev_start, paid_at__lte=prev_end
        ).aggregate(s=Sum('amount'))['s'] or 0
    )

    result = {
        'period_start': period_start.isoformat(),
        'period_end': period_end.isoformat(),
        'report_type': report_type,
        'total_activities': total,
        'completed': status_dist.get('done', 0),
        'in_progress': status_dist.get('in_progress', 0),
        'planned': status_dist.get('planned', 0),
        'cancelled': status_dist.get('cancelled', 0),
        'total_expense': total_expense,
        'expense_by_category': expense_by_cat,
        'daily_expense': daily_expense,
        'top_activities': top_activities,
        'top_tags': top_tags,
        'prev_period_expense': float(prev_expense),
    }

    if is_yearly:
        # ── 年度里程碑数据 ──
        # 打卡天数：区间内循环活动生成实例的完成数（单次聚合查询）
        result['checkin_days'] = activities.filter(
            recurring_source__isnull=False, status='done'
        ).count()

        # 分类费用最高项（基于已有 expense_by_cat，无额外查询）
        if expense_by_cat:
            top_cat, top_cat_amount = max(expense_by_cat.items(), key=lambda kv: kv[1])
            result['top_category'] = {'category': top_cat, 'amount': top_cat_amount}
        else:
            result['top_category'] = None

        # 最活跃月份：按月统计活动数（单次聚合查询）
        month_counts = dict(
            activities.filter(start_date__isnull=False)
            .annotate(m=TruncMonth('start_date'))
            .values_list('m').annotate(n=Count('id'))
            .values_list('m', 'n')
        )
        if month_counts:
            busiest = max(month_counts.items(), key=lambda kv: kv[1])
            result['most_active_month'] = {
                'month': busiest[0].strftime('%Y-%m'), 'count': busiest[1]
            }
        else:
            result['most_active_month'] = None

        result['monthly_expense'] = monthly_expense

    return result


def generate_report(user, report_type, period_start, period_end):
    """生成报告 Markdown 文本。

    先聚合数据，再调用 AI 生成分析文本，AI 失败时降级为纯数据模板。
    返回 (markdown_text, data_snapshot)。
    """
    data = collect_report_data(user, report_type, period_start, period_end)

    # 尝试 AI 生成
    try:
        markdown = _ai_generate_report(user, data, report_type, period_start, period_end)
        if markdown and len(markdown) > 50:
            return markdown, data
    except Exception as e:
        logger.warning(f'AI 报告生成失败: {e}')

    # 降级：纯数据模板
    markdown = _fallback_report(data, report_type, period_start, period_end)
    return markdown, data


def ai_round_trip(prompt, timeout=60):
    """公共 AI 会话往返：create_session → send_message → wait_for_response → finally cancel_session。

    返回 AI 回复文本（可能为空串）；无可用 Agent/Environment 配置时返回 None。
    异常向上抛出，由调用方捕获后降级处理。
    """
    from agents.services import get_service
    from agents.models import AgentConfig, EnvironmentConfig

    service = get_service()
    agent_config = AgentConfig.objects.filter(is_active=True).first()
    env_config = EnvironmentConfig.objects.filter(is_default=True).first() or EnvironmentConfig.objects.first()

    if not agent_config or not env_config:
        return None

    session_data = service.create_session(
        agent_id=agent_config.agent_id,
        environment_id=env_config.env_id,
    )
    try:
        service.send_message(session_data['id'], prompt)
        result = service.wait_for_response(session_data['id'], timeout=timeout)
        return result
    finally:
        try:
            service.cancel_session(session_data['id'])
        except Exception:
            pass


def _ai_generate_report(user, data, report_type, period_start, period_end):
    """调用 AI 生成报告分析文本"""
    type_label = TYPE_LABELS.get(report_type, '周报')
    period_str = f'{period_start.isoformat()} ~ {period_end.isoformat()}'

    if report_type == 'yearly':
        requirement = """要求：
1. 标题用 # 开头
2. 包含年度概述、里程碑回顾、费用分析、新年展望与建议四个部分
3. 费用分析要有同比对比，突出打卡天数、最活跃月份等里程碑数据
4. 语言简洁有洞察力，不要流水账
5. 总字数控制在 500-800 字"""
    else:
        requirement = """要求：
1. 标题用 # 开头
2. 包含概述、活动回顾、费用分析、亮点与建议四个部分
3. 费用分析要有环比对比
4. 语言简洁有洞察力，不要流水账
5. 总字数控制在 300-500 字"""

    prompt = f"""请根据以下数据生成一份{type_label}（{period_str}），用 Markdown 格式输出。

{requirement}

数据：
{json.dumps(data, ensure_ascii=False, indent=2)}"""

    return ai_round_trip(prompt, timeout=60)


def _fallback_report(data, report_type, period_start, period_end):
    """AI 失败时的纯数据模板报告"""
    from activities.models import Expense

    if report_type == 'yearly':
        return _fallback_yearly_report(data, period_start, period_end)

    type_label = TYPE_LABELS.get(report_type, '周报')

    lines = [
        f'# {type_label} · {period_start.strftime("%Y.%m.%d")} - {period_end.strftime("%m.%d")}',
        '',
        '## 概述',
        '',
        f'本周期共 {data["total_activities"]} 个活动，完成 {data["completed"]} 个，'
        f'进行中 {data["in_progress"]} 个，计划 {data["planned"]} 个，取消 {data["cancelled"]} 个。',
        '',
        '## 费用概览',
        '',
        f'总消费：**¥{data["total_expense"]:.0f}**',
    ]

    # 环比
    prev = data['prev_period_expense']
    if prev > 0:
        change = ((data['total_expense'] - prev) / prev) * 100
        direction = '增加' if change > 0 else '减少'
        lines.append(f'环比上周期{direction} {abs(change):.1f}%（上周期 ¥{prev:.0f}）')

    # 分类费用
    if data['expense_by_category']:
        lines.append('')
        lines.append('### 分类明细')
        lines.append('')
        lines.append('| 类别 | 金额 |')
        lines.append('|------|------|')
        cat_labels = dict(Expense.CATEGORY_CHOICES)
        for cat, amount in sorted(data['expense_by_category'].items(), key=lambda x: -x[1]):
            label = cat_labels.get(cat, cat)
            lines.append(f'| {label} | ¥{amount:.0f} |')

    # 亮点活动
    if data['top_activities']:
        lines.append('')
        lines.append('## 费用最高活动')
        lines.append('')
        for a in data['top_activities']:
            lines.append(f'- {a["name"]}：¥{a["amount"]:.0f}')

    return '\n'.join(lines)


def _fallback_yearly_report(data, period_start, period_end):
    """年报的纯数据降级模板（含年度里程碑）"""
    from activities.models import Expense

    cat_labels = dict(Expense.CATEGORY_CHOICES)

    lines = [
        f'# 年报 · {period_start.year}',
        '',
        '## 年度概述',
        '',
        f'全年共 {data["total_activities"]} 个活动，完成 {data["completed"]} 个，'
        f'进行中 {data["in_progress"]} 个，计划 {data["planned"]} 个，取消 {data["cancelled"]} 个。',
        '',
        '## 年度里程碑',
        '',
        f'- 总花费：**¥{data["total_expense"]:.0f}**',
        f'- 打卡天数：**{data.get("checkin_days", 0)}** 天',
    ]

    top_cat = data.get('top_category')
    if top_cat:
        label = cat_labels.get(top_cat['category'], top_cat['category'])
        lines.append(f'- 花费最高类别：**{label}**（¥{top_cat["amount"]:.0f}）')

    busiest = data.get('most_active_month')
    if busiest:
        lines.append(f'- 最活跃月份：**{busiest["month"]}**（{busiest["count"]} 个活动）')
    lines.append('')

    # 同比
    prev = data['prev_period_expense']
    if prev > 0:
        change = ((data['total_expense'] - prev) / prev) * 100
        direction = '增加' if change > 0 else '减少'
        lines.append(f'费用同比上年{direction} {abs(change):.1f}%（上年 ¥{prev:.0f}）')
        lines.append('')

    # 分类费用
    if data['expense_by_category']:
        lines.append('## 分类费用明细')
        lines.append('')
        lines.append('| 类别 | 金额 |')
        lines.append('|------|------|')
        for cat, amount in sorted(data['expense_by_category'].items(), key=lambda x: -x[1]):
            label = cat_labels.get(cat, cat)
            lines.append(f'| {label} | ¥{amount:.0f} |')
        lines.append('')

    # 每月费用趋势（有支出的月份）
    monthly = [m for m in data.get('monthly_expense', []) if m['amount'] > 0]
    if monthly:
        lines.append('## 每月费用')
        lines.append('')
        for m in monthly:
            lines.append(f'- {m["month"]}：¥{m["amount"]:.0f}')
        lines.append('')

    # 费用最高活动
    if data['top_activities']:
        lines.append('## 费用最高活动')
        lines.append('')
        for a in data['top_activities']:
            lines.append(f'- {a["name"]}：¥{a["amount"]:.0f}')

    return '\n'.join(lines)


def save_report_to_knowledge(user, report_type, title, content):
    """将报告保存为知识库 Article + 标签"""
    from knowledge.models import Article

    article = Article.objects.create(
        user=user,
        title=title,
        content=content,
    )
    tag_map = {'weekly': 'report-weekly', 'monthly': 'report-monthly', 'yearly': 'report-yearly'}
    article.tags.add(tag_map.get(report_type, 'report-monthly'))
    return article
