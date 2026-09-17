"""活动快速输入规则解析器

将一行文本结构化提取为活动字段，作为 AI 解析不可用/失败时的兜底。
只提取能明确识别的字段，不做猜测（返回值中不出现的 key = 未识别）。
"""
import re
from datetime import date, time, timedelta

from core.utils import week_monday

WEEK_MAP = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}

# 相对日期词 → 距今天的天数（包含过去侧：补记录靠它落在正确那一天）
RELATIVE_DAY_OFFSETS = {
    '大后天': 3, '后天': 2, '明天': 1, '今天': 0,
    '昨天': -1, '前天': -2, '大前天': -3,
}

# 星期的周偏移前缀（裸「周X」另处理：已过则顺延到下周）
WEEK_PREFIX_OFFSETS = {'下下周': 14, '下周': 7, '这周': 0, '本周': 0,
                       '上周': -7, '上上周': -14}

# 仅识别无歧义的状态词（避免「完成总结」这类误命中）
STATUS_KEYWORDS = [
    ('done', ('已完成',)),
    ('in_progress', ('进行中',)),
    ('cancelled', ('已取消',)),
]

# 金额提取：`kind` 区分「预算」（上限）与「费用/花了」（已花）；裸「X 元」没有 kind → 按已花处理
COST_PATTERN = re.compile(
    r'(?P<kind>预算|费用|花费|花销|开销|金额|花了)[^0-9]{0,6}(?P<num>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>千|万|元)?'
)
COST_YUAN_PATTERN = re.compile(r'(?P<num>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>元)')

# 时间表述：词头（凌晨/早上/…/晚上）+ 「X点半/X点Y分/X点」或「HH:MM」钟表格式
# 无词头的「X点」仅当小时 ≥ 13（如「15点」）才认定；「3点」歧义（凌晨/下午）不猜
TIME_PATTERN = re.compile(
    r'(凌晨|早晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*'
    r'(?:(\d{1,2})[点时](半|(\d{1,2})分?)?|(\d{1,2}):(\d{2}))'
)


def _convert_time(m):
    """时间匹配 → (hour, minute) | None（歧义/越界不猜）"""
    period, h, half, minutes, h24, m24 = m.groups()
    if h24 is not None:      # 钟表格式 HH:MM，本身就是 24 小时制
        h, mi = int(h24), int(m24)
    else:
        h, mi = int(h), (30 if half else int(minutes or 0))
        if period is None:
            if h < 13:       # 裸「3点」无词头，无法区分上下午 → 不猜
                return None
        elif period in ('下午', '傍晚', '晚上', '夜里') and h < 12:
            h += 12
        elif period == '中午' and h <= 2:
            h += 12          # 中午1点=13:00；中午12点保持 12
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def _overlap(span, spans):
    """判断区间与已占用区间是否重叠"""
    return any(not (span[1] <= s or span[0] >= e) for s, e in spans)


def _valid_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_quick_input(text, today=None):
    """解析快速输入文本，返回 dict（仅含识别出的字段，日期为 YYYY-MM-DD 字符串）"""
    today = today or date.today()
    spans = []      # 已识别片段区间（从名称中剔除）
    result = {}

    # 1. 标签 #xxx
    tag_matches = list(re.finditer(r'#([\w\u4e00-\u9fa5]+)', text))
    if tag_matches:
        result['tags'] = [m.group(1) for m in tag_matches]
        spans += [m.span() for m in tag_matches]

    # 2. 参与者 @xxx
    at_matches = list(re.finditer(r'@([\w\u4e00-\u9fa5]+)', text))
    if at_matches:
        result['participants'] = [m.group(1) for m in at_matches]
        spans += [m.span() for m in at_matches]

    # 3. 状态关键词
    for status, words in STATUS_KEYWORDS:
        hit = None
        for w in words:
            idx = text.find(w)
            if idx != -1:
                hit = (idx, idx + len(w))
                break
        if hit:
            result['status'] = status
            spans.append(hit)
            break

    # 4. 金额：识别已花费用；「预算 X」语义（预算上限）已随 Activity.budget 字段移除，仅占位不写入
    cost_match = COST_PATTERN.search(text)
    if not cost_match:
        cost_match = COST_YUAN_PATTERN.search(text)
    if cost_match and not _overlap(cost_match.span(), spans):
        g = cost_match.groupdict()
        cost = float(g['num'])
        if g.get('unit') == '千':
            cost *= 1000
        elif g.get('unit') == '万':
            cost *= 10000
        if g.get('kind') != '预算':
            result['cost'] = cost
        spans.append(cost_match.span())

    # 5. 日期收集（按文本出现顺序），识别后占位避免重复匹配
    found = []      # [(date, span)]

    def add_dates(dates, span):
        if _overlap(span, spans + [s for _, s in found]):
            return
        found.extend((d, span) for d in dates)
        spans.append(span)

    # YYYY-MM-DD / YYYY/M/D
    for m in re.finditer(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', text):
        d = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            add_dates([d], m.span())
    # YYYY年M月D日
    for m in re.finditer(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?', text):
        d = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            add_dates([d], m.span())
    # X月Y日到Z日（同月区间；日后再跟「月」则属于跨月，交由两个单日期模式分别匹配）
    for m in re.finditer(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*(?:到|至|—|-|~)\s*(\d{1,2})\s*[日号]?(?!\s*月)', text):
        d1 = _valid_date(today.year, int(m.group(1)), int(m.group(2)))
        d2 = _valid_date(today.year, int(m.group(1)), int(m.group(3)))
        if d1 and d2:
            add_dates([d1, d2], m.span())
    # 单个 M月D日/号（默认当年）
    for m in re.finditer(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]', text):
        d = _valid_date(today.year, int(m.group(1)), int(m.group(2)))
        if d:
            add_dates([d], m.span())
    # 相对日期（向后看与向前看同口径：补记录「昨天/前天花了多少」这类输入
    # 不能丢日期，否则费用落不到当天，今日/本周统计就少一笔）
    for m in re.finditer(r'大后天|大前天|后天|前天|明天|今天|昨天', text):
        add_dates([today + timedelta(days=RELATIVE_DAY_OFFSETS[m.group(0)])], m.span())
    for m in re.finditer(r'(\d{1,2})\s*天[后前]', text):
        n = int(m.group(1))
        add_dates([today + timedelta(days=n if m.group(0).endswith('后') else -n)],
                  m.span())
    # 下周X / 上周X / 这周X / 裸周X（裸周X已过则顺延到下周；带前缀的由第一分支匹配）
    for m in re.finditer(
            r'(下下周|下周|上上周|上周|这周|本周)([一二三四五六日天])'
            r'|(?<![下这本上])周([一二三四五六日天])', text):
        prefix = m.group(1)
        target = WEEK_MAP[m.group(2) or m.group(3)]
        monday = week_monday(today)
        if prefix:
            d = monday + timedelta(days=WEEK_PREFIX_OFFSETS[prefix] + target)
        else:
            d = monday + timedelta(days=target)
            if d < today:
                d += timedelta(days=7)
        add_dates([d], m.span())
    # 月底
    for m in re.finditer(r'月底', text):
        month_end = date(today.year, today.month, 1) + timedelta(days=32)
        add_dates([month_end.replace(day=1) - timedelta(days=1)], m.span())

    # 5.5 时间收集（第一个→开始时间，第二个→结束时间；识别后从名称剔除。
    # 只有同时识别到日期才输出：孤时间无法落到日历，与其存怪数据不如不猜）
    times = []
    for m in TIME_PATTERN.finditer(text):
        converted = _convert_time(m)
        if converted and not _overlap(m.span(), spans):
            times.append((converted, m.span()))
            spans.append(m.span())

    if found:
        found.sort(key=lambda item: item[1][0])
        start, end = found[0][0], found[-1][0]
        if end < start:
            start, end = end, start
        result['start_date'] = start.isoformat()
        result['end_date'] = end.isoformat()
        for (h, mi), _span in times[:2]:
            key = 'start_time' if 'start_time' not in result else 'end_time'
            result[key] = time(h, mi).strftime('%H:%M')

    # 6. 名称：剔除全部已识别片段后的剩余文本
    chars = list(text)
    for s, e in spans:
        for i in range(s, e):
            chars[i] = ''
    rest = re.sub(r'\s+', ' ', ''.join(chars)).strip()
    rest = rest.strip('，,.;；:：!！?？')
    rest = re.sub(r'^[到至从去在的和与,，、\s]+', '', rest)
    rest = re.sub(r'[的和与,，、\s]+$', '', rest)
    if rest:
        result['name'] = rest

    return result
