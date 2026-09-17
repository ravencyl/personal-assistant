"""ICS 日历订阅源生成（纯标准库，无第三方依赖，2026-09-11）。

方案取舍：单向「站点 → Apple 日历」导出选 ICS/webcal 订阅而非 CalDAV——
CalDAV 需要完整协议栈（PROPFIND/REPORT/etag 同步、鉴权挑战）与重型依赖，
而 Apple 日历对订阅源会自行定期刷新，足够覆盖「不错过截止时间」的诉求。

导出范围与映射约定：
- 只导出 planned / in_progress 且能落到具体日期的活动（含匹配条件的子任务）；
  done / cancelled 不进日历，保持日历干净。
- 全天事件：DTSTART/DTEND 用 DATE 值；RFC 5545 规定 DTEND 为排他日期，
  故跨天活动导出为 end_date + 1 天。
- 带具体时间的活动（2026-09-17）：DTSTART/DTEND 用 DATE-TIME 值，
  统一转 UTC（Z 后缀，无需 VTIMEZONE 块），Apple 日历会在准确时刻提醒；
  结束时间缺省时按「跨度 = 日期差 + 1 小时」收尾（跨天用同时间补足整天）。
- 全天/定点两种模式共存：只要 start_time 为空就仍按全天导出。
- 状态映射：in_progress → STATUS:CONFIRMED，planned → STATUS:TENTATIVE；
  CATEGORIES 写状态中文，便于在日历 App 里按分类辨识。
- 详情页链接同时写入 URL 与 DESCRIPTION，方便从日历跳回站点。
- 每个 VEVENT 附带 VALARM（提前 1 天提醒）；订阅日历若未触发，
  可在 Apple 日历中为该订阅日历设置「默认提醒」兜底。
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from core.utils import visible_qs
from .models import Activity

# 可导出的状态 → STATUS/CATEGORIES 映射
_STATUS_MAP = {
    'planned': ('TENTATIVE', '计划'),
    'in_progress': ('CONFIRMED', '进行中'),
}

_DESCRIPTION_MAX = 500  # DESCRIPTION 截断长度，避免日历条目过长


def _escape_text(text):
    """RFC 5545 TEXT 转义：反斜杠、分号、逗号、换行。"""
    return (str(text)
            .replace('\\', '\\\\')
            .replace(';', '\\;')
            .replace(',', '\\,')
            .replace('\r\n', '\\n')
            .replace('\n', '\\n'))


def _fold(line):
    """按 RFC 5545 把超长行折叠为 75 字节（UTF-8 octet，不拆多字节字符）。"""
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return line
    parts = []
    start, limit = 0, 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # 不在多字节序列中间断开
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(raw[start:end].decode('utf-8'))
        start = end
        limit = 74  # 续行以空格开头，占 1 字节
    return '\r\n '.join(parts)


def _date_param(d):
    """DATE 值（全天事件）。"""
    return d.strftime('%Y%m%d')


def _datetime_param(aware_dt):
    """DATE-TIME 值（定点事件）：统一转 UTC，Z 后缀，避免 VTIMEZONE 块。"""
    # 注意：django.utils.timezone.utc 在 Django 5 已移除，用标准库的
    return aware_dt.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _timed_datetimes(activity):
    """定点活动的 (start_dt, end_dt)；start_time 未设返回 None（维持全天导出）。"""
    if not activity.start_time:
        return None
    start = activity.start_date or activity.end_date
    end = activity.end_date or activity.start_date
    if start is None or end is None:
        return None
    start_dt = timezone.make_aware(datetime.combine(start, activity.start_time))
    if activity.end_time:
        end_dt = timezone.make_aware(datetime.combine(end, activity.end_time))
    else:
        # 未填结束时间：按「日期跨度 + 1 小时」收尾（当天 1 小时，跨天补足整天）
        end_dt = start_dt + timedelta(days=(end - start).days, hours=1)
    if end_dt <= start_dt:   # 同刻/倒挂兑底：保底 1 小时，不产生零时长/负时长事件
        end_dt = start_dt + timedelta(hours=1)
    return start_dt, end_dt


def exportable_activities(user):
    """可导出到日历的活动集合（按用户隔离，遵守可见性约定）。"""
    today = timezone.localdate()
    return (visible_qs(Activity, user)
            .filter(status__in=_STATUS_MAP)
            .filter(end_date__gte=today)
            .select_related('parent')
            .order_by('start_date', 'id'))


def _event_lines(activity, base_url):
    """单个 VEVENT 的行列表；无法落到具体日期时返回 None。"""
    start = activity.start_date or activity.end_date
    end = activity.end_date or activity.start_date
    if start is None or end is None:
        return None

    status, status_label = _STATUS_MAP[activity.status]
    detail_url = f"{base_url}/activities/{activity.pk}/"
    desc_parts = []
    if activity.description:
        desc_parts.append(activity.description[:_DESCRIPTION_MAX])
    desc_parts.append(f'详情: {detail_url}')
    desc_parts.append(f'状态: {status_label}')
    if activity.parent_id:
        desc_parts.append(f'子任务，父活动: {activity.parent.name}')

    # 注意：表达式内不能含反斜杠（线上 Python 3.11 的 f-string 不支持）
    description = _escape_text('\n'.join(desc_parts))
    # iCloud 日历不展示 CATEGORIES，状态直接拼在名称后（2026-09-12 用户要求）
    summary = f'{_escape_text(activity.name)} - [{status_label}]'

    timed = _timed_datetimes(activity)
    if timed:
        start_dt, end_dt = timed
        dtstart = f'DTSTART:{_datetime_param(start_dt)}'
        dtend = f'DTEND:{_datetime_param(end_dt)}'
        alarm_trigger = 'TRIGGER:-PT30M'   # 定点事件提前半小时提醒
    else:
        dtstart = f'DTSTART;VALUE=DATE:{_date_param(start)}'
        # RFC 5545：DTEND 为排他日期
        dtend = f'DTEND;VALUE=DATE:{_date_param(end + timedelta(days=1))}'
        alarm_trigger = 'TRIGGER:-P1D'

    lines = [
        'BEGIN:VEVENT',
        f'UID:activity-{activity.pk}@ravenclaw.top',
        f'DTSTAMP:{timezone.now():%Y%m%dT%H%M%SZ}',
        dtstart,
        dtend,
        f'SUMMARY:{summary}',
        f'DESCRIPTION:{description}',
        f'URL:{detail_url}',
        f'CATEGORIES:{_escape_text(status_label)}',
        f'STATUS:{status}',
        f'LAST-MODIFIED:{activity.updated_at:%Y%m%dT%H%M%SZ}',
        'BEGIN:VALARM',
        'ACTION:DISPLAY',
        f'DESCRIPTION:{_escape_text(activity.name)}',
        alarm_trigger,
        'END:VALARM',
        'END:VEVENT',
    ]
    return lines


def build_ics(user, base_url):
    """生成完整 VCALENDAR 文本（CRLF 行尾 + 75 字节折叠）。"""
    base_url = base_url.rstrip('/')
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//ravenclaw.top//Personal Assistant//CN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:活动日历',
        'X-PUBLISHED-TTL:PT1H',  # 非标准提示：建议每小时刷新（Apple 会忽略）
    ]
    for activity in exportable_activities(user):
        event = _event_lines(activity, base_url)
        if event:
            lines.extend(event)
    lines.append('END:VCALENDAR')
    return '\r\n'.join(_fold(line) for line in lines) + '\r\n'
