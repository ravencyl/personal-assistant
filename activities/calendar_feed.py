"""ICS 日历订阅源生成（纯标准库，无第三方依赖，2026-09-11）。

方案取舍：单向「站点 → Apple 日历」导出选 ICS/webcal 订阅而非 CalDAV——
CalDAV 需要完整协议栈（PROPFIND/REPORT/etag 同步、鉴权挑战）与重型依赖，
而 Apple 日历对订阅源会自行定期刷新，足够覆盖「不错过截止时间」的诉求。

导出范围与映射约定：
- 只导出 planned / in_progress 且能落到具体日期的活动（含匹配条件的子任务）；
  done / cancelled 不进日历，保持日历干净。
- 全天事件：DTSTART/DTEND 用 DATE 值；RFC 5545 规定 DTEND 为排他日期，
  故跨天活动导出为 end_date + 1 天。
- 状态映射：in_progress → STATUS:CONFIRMED，planned → STATUS:TENTATIVE；
  CATEGORIES 写状态中文，便于在日历 App 里按分类辨识。
- 详情页链接同时写入 URL 与 DESCRIPTION，方便从日历跳回站点。
- 每个 VEVENT 附带 VALARM（提前 1 天提醒）；订阅日历若未触发，
  可在 Apple 日历中为该订阅日历设置「默认提醒」兜底。
"""
from datetime import timedelta

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

    lines = [
        'BEGIN:VEVENT',
        f'UID:activity-{activity.pk}@ravenclaw.top',
        f'DTSTAMP:{timezone.now():%Y%m%dT%H%M%SZ}',
        f'DTSTART;VALUE=DATE:{_date_param(start)}',
        # RFC 5545：DTEND 为排他日期
        f'DTEND;VALUE=DATE:{_date_param(end + timedelta(days=1))}',
        f'SUMMARY:{_escape_text(activity.name)}',
        f'DESCRIPTION:{description}',
        f'URL:{detail_url}',
        f'CATEGORIES:{_escape_text(status_label)}',
        f'STATUS:{status}',
        f'LAST-MODIFIED:{activity.updated_at:%Y%m%dT%H%M%SZ}',
        'BEGIN:VALARM',
        'ACTION:DISPLAY',
        f'DESCRIPTION:{_escape_text(activity.name)}',
        'TRIGGER:-P1D',
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
