"""提醒 Agent 工具

注册 set_reminder 和 list_reminders 意图工具。
"""
import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.agent_registry import agent_tool, ToolError
from core.models import Reminder

logger = logging.getLogger(__name__)


@agent_tool('reminders.set_reminder', '设置定时提醒',
            'content（提醒内容，必填）+ remind_at（触发时间，ISO 格式 YYYY-MM-DDTHH:MM:SS，必填）+ activity_target（关联活动名称关键词，可选）')
def tool_set_reminder(user, params):
    content = params.get('content', '').strip()
    if not content:
        raise ToolError('请告诉我提醒内容')

    remind_at_str = params.get('remind_at', '').strip()
    if not remind_at_str:
        raise ToolError('请告诉我提醒时间')

    remind_at = parse_datetime(remind_at_str)
    if remind_at is None:
        raise ToolError('时间格式不正确，请使用 YYYY-MM-DDTHH:MM:SS 格式')

    # 确保时区感知
    if timezone.is_naive(remind_at):
        remind_at = timezone.make_aware(remind_at)

    # 可选：关联活动
    related_activity = None
    activity_target = params.get('activity_target', '').strip()
    if activity_target:
        from activities.models import Activity
        from core.utils import visible_qs
        matches = visible_qs(Activity, user).filter(name__icontains=activity_target)
        if matches.count() == 1:
            related_activity = matches.first()

    reminder = Reminder.objects.create(
        user=user,
        content=content,
        trigger_at=remind_at,
        related_activity=related_activity,
    )

    time_display = remind_at.strftime('%m月%d日 %H:%M')
    return {
        'reply': f'已设置提醒：{time_display} — {content}',
        'card': 'reminder',
        'card_data': {
            'content': content,
            'trigger_at': remind_at.isoformat(),
            'time_display': time_display,
            'reminder_id': reminder.id,
        },
        'changed': True,
    }


@agent_tool('reminders.list_reminders', '查看提醒列表',
            'status（可选，默认 pending：待触发/fired：已触发/dismissed：已忽略）')
def tool_list_reminders(user, params):
    status = params.get('status', 'pending').strip()
    if status not in ('pending', 'fired', 'dismissed'):
        status = 'pending'

    reminders = Reminder.objects.filter(user=user, status=status).order_by('trigger_at')[:10]

    if not reminders:
        status_label = dict(Reminder.STATUS_CHOICES).get(status, status)
        return {
            'reply': f'没有{status_label}的提醒',
        }

    lines = []
    for r in reminders:
        time_display = r.trigger_at.strftime('%m/%d %H:%M')
        lines.append(f'- [{time_display}] {r.content}')

    status_label = dict(Reminder.STATUS_CHOICES).get(status, status)
    return {
        'reply': f'{status_label}的提醒（{len(lines)} 条）：\n' + '\n'.join(lines),
    }
