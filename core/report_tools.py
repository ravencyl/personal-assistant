"""报告生成 Agent 工具"""
import logging
from datetime import date, timedelta
from django.utils import timezone
from core.agent_registry import agent_tool, ToolError
from core.utils import week_monday
from core.report_generator import generate_report, save_report_to_knowledge

logger = logging.getLogger(__name__)


@agent_tool('reports.generate', '生成周报、月报或年报',
            'report_type（weekly / monthly / yearly，必填）；year（仅 yearly 可选，年份，缺省当年）')
def tool_generate_report(user, params):
    report_type = params.get('report_type', '').strip()
    if report_type not in ('weekly', 'monthly', 'yearly'):
        raise ToolError('请指定报告类型：weekly（周报）、monthly（月报）或 yearly（年报）')

    today = timezone.localdate()
    if report_type == 'weekly':
        period_start = week_monday(today)
        period_end = today
        type_label = '周报'
    elif report_type == 'monthly':
        period_start = today.replace(day=1)
        period_end = today
        type_label = '月报'
    else:
        # 年报：年份缺省当年，可查往年（不晚于当年）
        try:
            year = int(params.get('year') or today.year)
        except (TypeError, ValueError):
            year = today.year
        if year < 2000 or year > today.year:
            year = today.year
        period_start = date(year, 1, 1)
        period_end = date(year, 12, 31) if year < today.year else today
        type_label = '年报'

    markdown, data = generate_report(user, report_type, period_start, period_end)

    # 自动保存到知识库
    if report_type == 'weekly':
        title = f'{type_label} · {today.year}年第{period_start.isocalendar()[1]}周'
    elif report_type == 'monthly':
        title = f'{type_label} · {today.year}年{today.month}月'
    else:
        title = f'{type_label} · {period_start.year}年'

    article = save_report_to_knowledge(user, report_type, title, markdown)

    return {
        'reply': f'已生成{type_label}并保存到知识库：{title}',
        'card': 'report',
        'card_data': {
            'title': title,
            'summary': markdown[:200] if markdown else '',
            'article_slug': article.slug,
        },
        'changed': True,
    }
