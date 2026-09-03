"""提醒 Agent 工具

注册 set_reminder、list_reminders、complete_reminder 意图工具。
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


def _fmt_reminders(reminders):
    """提醒列表的展示行（两种口径共用，不再各写一遍 strftime）"""
    return '\n'.join(
        f'- [{r.trigger_at.strftime("%m/%d %H:%M")}] {r.content}' for r in reminders)


@agent_tool('reminders.list_reminders', '查看提醒列表',
            'status（可选）：不传 = 待处理（到点了还没处理掉的）加今天稍后的预告；'
            '也可显式指定 pending（待触发）/fired（已触发未处理）/done（已完成）/dismissed（已忽略）')
def tool_list_reminders(user, params):
    from core.utils import pending_reminders, upcoming_reminders

    valid = dict(Reminder.STATUS_CHOICES)
    status = (params.get('status') or '').strip()
    if status in valid:
        # 显式指定某一状态：按字面查（用户确实在问「我完成过哪些提醒」这类）
        reminders = list(
            Reminder.objects.filter(user=user, status=status).order_by('trigger_at')[:10])
        if not reminders:
            return {'reply': f'没有{valid[status]}的提醒'}
        return {'reply': f'{valid[status]}的提醒（{len(reminders)} 条）：\n' + _fmt_reminders(reminders)}

    # 默认口径：待处理 + 今天稍后，两段分开标数。
    # 为什么不只答「待处理」：用户问「我有什么提醒」时想知道今天全部安排，
    # 只答已到点的会在没有任何逾期提醒时回一句「没有」，而今晚确实有个会。
    # 为什么不混成一个列表：「待处理」那一段的条数必须等于页面红点数，
    # 分开标数才能既不漏信息又不与红点自相矛盾（以前不传 status 查字面
    # pending，提醒一旦到期被改成 fired 就答「没有」，红点却亮着 1）。
    # 非法 status 同样落到这里，与旧行为（静默退回默认）一致的容错。
    pending = list(pending_reminders(user)[:10])
    upcoming = list(upcoming_reminders(user)[:10])
    if not pending and not upcoming:
        return {'reply': '没有待处理的提醒，今天也没有新的'}

    blocks = []
    if pending:
        blocks.append(f'待处理（{len(pending)} 条）：\n' + _fmt_reminders(pending))
    if upcoming:
        blocks.append(f'今天稍后（{len(upcoming)} 条）：\n' + _fmt_reminders(upcoming))
    return {'reply': '\n'.join(blocks)}


@agent_tool('reminders.complete', '确认完成/忽略一条提醒',
            'reminder_id（提醒 ID，必填）或 target（提醒内容关键词）')
def tool_complete_reminder(user, params):
    reminder_id = params.get('reminder_id')
    reminder = None
    if reminder_id:
        reminder = Reminder.objects.filter(user=user, id=reminder_id).first()
    else:
        target = str(params.get('target') or '').strip()
        if not target:
            raise ToolError('请告诉我提醒 ID 或内容关键词')
        # 把 done 也纳入匹配：否则用户说「那条已完成的提醒已完成」会得到「没找到」，
        # 不如由下方分支明确告知「之前已处理过」
        matches = Reminder.objects.filter(user=user, content__icontains=target,
                                          status__in=('pending', 'fired', 'done'))
        if matches.count() > 1:
            raise ToolError(f'匹配到 {matches.count()} 条提醒，请说得更具体些')
        reminder = matches.first()
    if not reminder:
        raise ToolError('没有找到这条提醒，可能已处理或删除')
    if reminder.status in ('dismissed', 'done'):
        return {'reply': f'提醒「{reminder.content}」之前已经处理过了'}

    reminder.status = 'dismissed'
    reminder.save(update_fields=['status'])
    return {
        'reply': f'已确认提醒「{reminder.content}」，不再提醒',
        'changed': True,
    }
