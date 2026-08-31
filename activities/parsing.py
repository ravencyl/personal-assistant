"""活动快速输入规则解析器

将一行文本结构化提取为活动字段，作为 AI 解析不可用/失败时的兜底。
只提取能明确识别的字段，不做猜测（返回值中不出现的 key = 未识别）。
"""
import re
from datetime import date, timedelta

from core.utils import week_monday

WEEK_MAP = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}

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

    # 4. 金额：「预算 X」写预算上限，其余写已花费用（两者语义不同，绝不互写）
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
        result['budget' if g.get('kind') == '预算' else 'cost'] = cost
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
    # 相对日期
    for m in re.finditer(r'大后天|后天|明天|今天', text):
        offset = {'今天': 0, '明天': 1, '后天': 2, '大后天': 3}[m.group(0)]
        add_dates([today + timedelta(days=offset)], m.span())
    for m in re.finditer(r'(\d{1,2})\s*天后', text):
        add_dates([today + timedelta(days=int(m.group(1)))], m.span())
    # 下周X / 这周X / 本周X / 裸周X（裸周X已过则顺延到下周；带前缀的由第一分支匹配）
    for m in re.finditer(r'(下周|这周|本周)([一二三四五六日天])|(?<![下这本])周([一二三四五六日天])', text):
        prefix = m.group(1)
        target = WEEK_MAP[m.group(2) or m.group(3)]
        monday = week_monday(today)
        if prefix == '下周':
            d = monday + timedelta(days=7 + target)
        else:
            d = monday + timedelta(days=target)
            if not prefix and d < today:
                d += timedelta(days=7)
        add_dates([d], m.span())
    # 月底
    for m in re.finditer(r'月底', text):
        month_end = date(today.year, today.month, 1) + timedelta(days=32)
        add_dates([month_end.replace(day=1) - timedelta(days=1)], m.span())

    if found:
        found.sort(key=lambda item: item[1][0])
        start, end = found[0][0], found[-1][0]
        if end < start:
            start, end = end, start
        result['start_date'] = start.isoformat()
        result['end_date'] = end.isoformat()

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
