"""报告生成 Agent 工具"""
import logging
from datetime import timedelta
from django.utils import timezone
from core.agent_registry import agent_tool, ToolError
from core.report_generator import generate_report, save_report_to_knowledge

logger = logging.getLogger(__name__)


@agent_tool('reports.generate', '生成周报或月报',
            'report_type（weekly 或 monthly，必填）')
def tool_generate_report(user, params):
    report_type = params.get('report_type', '').strip()
    if report_type not in ('weekly', 'monthly'):
        raise ToolError('请指定报告类型：weekly（周报）或 monthly（月报）')

    today = timezone.localdate()
    if report_type == 'weekly':
        period_start = today - timedelta(days=today.weekday())
        period_end = today
        type_label = '周报'
    else:
        period_start = today.replace(day=1)
        period_end = today
        type_label = '月报'

    markdown, data = generate_report(user, report_type, period_start, period_end)

    # 自动保存到知识库
    if report_type == 'weekly':
        title = f'{type_label} · {today.year}年第{period_start.isocalendar()[1]}周'
    else:
        title = f'{type_label} · {today.year}年{today.month}月'

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
