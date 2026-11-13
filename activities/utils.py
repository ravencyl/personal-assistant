"""活动模块共用工具函数

供 views.py 与 agent_tools.py 共同引用，避免跨模块导入私有函数。
"""
import logging
import re
from datetime import date

from django.db import models
from django.db.models import Sum

from core.tags import tag_names
from core.utils import visible_qs, q_or
from .models import Activity, ActivityLog, Expense, Participant

logger = logging.getLogger(__name__)


# ==================== 「日常开支」归属桶 ====================
# 桶活动为系统常驻活动，用于承接无活动语境的费用；
# 识别方式：标题 == DAILY_BUCKET_NAME 且描述含 DAILY_BUCKET_MARKER（比普通用户同名活动更稳妥）。
DAILY_BUCKET_NAME = '日常开支'
DAILY_BUCKET_MARKER = '系统归属桶：无活动语境的费用记入此处'


def daily_bucket_q():
    """归属桶的唯一识别条件（标题 + 描述里的 marker）

    取桶 / 内存判定 / 查询集排除三处必须共用这一个条件：以前取桶只按 name、
    排除按 name+marker，用户手工建一个同名活动就会被静默收养成系统桶，
    既出现在列表里又充当费用归属对象。
    """
    return models.Q(name=DAILY_BUCKET_NAME, description__contains=DAILY_BUCKET_MARKER)


def get_daily_bucket(user):
    """惰性创建/复用该用户的「日常开支」归属桶活动

    按 marker 查找而不是按 name get_or_create：命中不到就新建一条带 marker 的，
    用户自建的同名普通活动不会被当成系统桶。
    """
    bucket = Activity.objects.filter(user=user).filter(daily_bucket_q()).first()
    if bucket is not None:
        return bucket
    return Activity.objects.create(
        user=user,
        name=DAILY_BUCKET_NAME,
        description=DAILY_BUCKET_MARKER,
        status='in_progress',
        start_date=None,
        end_date=None,
    )


def is_daily_bucket(activity):
    """判断活动是否为「日常开支」归属桶（daily_bucket_q 的内存版同口径，由测试交叉校验）"""
    return (
        activity.name == DAILY_BUCKET_NAME
        and DAILY_BUCKET_MARKER in (activity.description or '')
    )


def exclude_daily_bucket(qs):
    """从活动查询集中排除归属桶（列表页/每日简报展示用；费用统计不排除）"""
    return qs.exclude(daily_bucket_q())


# ==================== 参与者解析 ====================

def resolve_participants(user, names, create_missing=False):
    """按姓名解析参与者对象（不区分大小写、忽略首尾空白与 @ 前缀）

    create_missing=False（AI 自动识别路径）：只填已存在的参与者，匹配不到就跳过，
    避免把「yyx」这类大小写变体建成新联系人。
    create_missing=True（用户手动填写路径）：先复用已有写法，确实没有才新建，
    因此手输 yyx 会归到已存在的 YYX 而不是新开一条。

    返回 (participants, skipped_names, created_names)。
    """
    wanted, seen = [], set()
    for raw in names or []:
        name = str(raw).strip().lstrip('@').strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            wanted.append(name)
    if not wanted:
        return [], [], []

    index = {}
    for p in Participant.objects.filter(user=user):
        index.setdefault(p.name.strip().lower(), p)

    participants, skipped, created = [], [], []
    for name in wanted:
        p = index.get(name.lower())
        if p is None and create_missing:
            p = Participant.objects.create(user=user, name=name)
            index[name.lower()] = p
            created.append(p.name)
        if p is None:
            skipped.append(name)
            continue
        if p not in participants:
            participants.append(p)
    return participants, skipped, created


def _norm_participant_name(raw):
    """参与者姓名归一化：去首尾空白、去 @ 前缀、转小写、内部空白压成单空格。

    与 resolve_participants 的匹配键同源（strip + lower + 去 @），额外折叠内部
    空白，使「KeYing  Li」与「keying li」归一，供精确/分词/子串三级匹配共用。
    """
    text = str(raw or '').strip().lstrip('@').strip().lower()
    return ' '.join(text.split())


# 模糊匹配三级证据强度（强→弱）：精确相等 > 整词命中 > 双向子串。
# 只取「有命中的最强一级」判唯一/歧义，避免弱证据把强匹配稀释成歧义。
_FUZZY_TIERS = ('exact', 'token', 'substring')
# 子串级最短长度：单字（中文单姓、英文单字母）太易误伤，不参与子串匹配
_FUZZY_MIN_SUBSTR = 2


def fuzzy_resolve_participants(user, names):
    """只读模糊解析：把口语名字匹配到已有联系人，分三类返回，绝不写库/新建。

    与 resolve_participants 的分工：那个是「精确匹配 + 唯一写入口」，这个是
    「AI 路径的识别增强」——用户说「keying」而联系人里是「KeYing Li」时，
    精确匹配落空，这里按三级证据把它认出来，交给调用方决定如何向用户复述/确认。

    三级证据（强→弱，只取有命中的最强一级）：
    - exact：归一化后整名相等（「keying li」==「KeYing Li」）
    - token：口语名是联系人某个整词，或联系人是口语名某个整词（「keying」→「KeYing Li」）
    - substring：双向子串包含，双方长度均 ≥2（中文「可盈」→「李可盈」）

    刻意不用 char_overlap_ratio：字符重合度会把「Joey」误判到「Joe Yan」
    （AGENTS.md 明令二者是不同的人）。整词/子串是结构证据，不是相似度赌注。

    返回 dict：
      matched   [(spoken, Participant, kind)]  唯一命中（kind ∈ exact/token/substring）
      ambiguous [(spoken, [Participant, ...])]  最强一级命中多个，需用户澄清
      missing   [spoken, ...]                   三级全落空，确认不存在后才可议新建
    spoken 保留用户原始写法（去 @/首尾空白），供回复复述；同一联系人被多个口语名
    命中时不去重（由调用方按 participant.id 收敛）。
    """
    wanted, seen = [], set()
    for raw in names or []:
        spoken = str(raw).strip().lstrip('@').strip()
        norm = _norm_participant_name(spoken)
        if norm and norm not in seen:
            seen.add(norm)
            wanted.append((spoken, norm))
    result = {'matched': [], 'ambiguous': [], 'missing': []}
    if not wanted:
        return result

    contacts = [(p, _norm_participant_name(p.name))
                for p in Participant.objects.filter(user=user)]
    for spoken, norm in wanted:
        tiers = {'exact': [], 'token': [], 'substring': []}
        spoken_tokens = set(norm.split())
        for p, cnorm in contacts:
            if not cnorm:
                continue
            if norm == cnorm:
                tiers['exact'].append(p)
            elif norm in cnorm.split() or cnorm in spoken_tokens:
                tiers['token'].append(p)
            elif (len(norm) >= _FUZZY_MIN_SUBSTR and len(cnorm) >= _FUZZY_MIN_SUBSTR
                  and (norm in cnorm or cnorm in norm)):
                tiers['substring'].append(p)
        for kind in _FUZZY_TIERS:
            hits = tiers[kind]
            if not hits:
                continue
            if len(hits) == 1:
                result['matched'].append((spoken, hits[0], kind))
            else:
                result['ambiguous'].append((spoken, hits))
            break
        else:
            result['missing'].append(spoken)
    return result


def log_activity(user, activity, action, summary=''):
    """写入活动操作日志（失败仅告警，不影响主流程）"""
    try:
        ActivityLog.objects.create(
            user=user,
            activity=activity,
            activity_name=activity.name,
            action=action,
            summary=summary,
        )
    except Exception as e:
        logger.warning(f'活动日志写入失败: {e}')


def snapshot_activity(activity):
    """编辑前字段快照，用于生成变更摘要"""
    return {
        'name': activity.name,
        'description': activity.description,
        'start_date': activity.start_date,
        'end_date': activity.end_date,
        'start_time': activity.start_time,
        'end_time': activity.end_time,
        'status': activity.status,
        'parent': activity.parent,
        'tags': set(tag_names(activity)),
        'participants': set(activity.participants.values_list('name', flat=True)),
    }


_EDIT_FIELDS = [
    ('name', '名称'), ('description', '描述'),
    ('start_date', '开始日期'), ('end_date', '结束日期'),
    ('start_time', '开始时间'), ('end_time', '结束时间'),
    ('status', '状态'),
]


def fmt_field(field, value):
    """格式化字段值用于变更摘要展示"""
    if value in (None, ''):
        return '空'
    if field == 'status':
        return dict(Activity.STATUS_CHOICES).get(value, str(value))
    if field in ('start_time', 'end_time') and hasattr(value, 'strftime'):
        return value.strftime('%H:%M')
    text = str(value)
    if field == 'description':
        # 描述可能很长：变更摘要/活动日志里只留头一段（折行压成空格），
        # 整段贴进日志会撑爆时间线，也能避免被当作 AI 回复全文输出
        flat = ' '.join(text.split())
        return flat if len(flat) <= 40 else f'{flat[:40]}…'
    return text


def diff_part(label, old_set, new_set):
    """对比集合差异，生成 +/- 描述文本"""
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    parts = []
    if added:
        parts.append('+' + '、'.join(added))
    if removed:
        parts.append('-' + '、'.join(removed))
    return f'{label}{" ".join(parts)}' if parts else ''


def edit_summary(old, activity):
    """对比编辑前后快照，生成变更摘要"""
    changes = []
    for field, label in _EDIT_FIELDS:
        new_v = getattr(activity, field)
        if old[field] != new_v:
            changes.append(f'{label} {fmt_field(field, old[field])} → {fmt_field(field, new_v)}')
    if old['parent'] != activity.parent:
        old_p = old['parent'].name if old['parent'] else '无'
        new_p = activity.parent.name if activity.parent else '无'
        changes.append(f'父活动 {old_p} → {new_p}')
    tag_diff = diff_part('标签 ', old['tags'], set(tag_names(activity)))
    if tag_diff:
        changes.append(tag_diff)
    p_diff = diff_part('参与者 ', old['participants'],
                       set(activity.participants.values_list('name', flat=True)))
    if p_diff:
        changes.append(p_diff)
    return '；'.join(changes)[:500]


_TIME_HHMM = re.compile(r'^(\d{1,2}):(\d{2})')

# 时段词 → 时段区间（start_time 取区间起点、end_time 取区间终点）。
# 区间口径与协议规则 15 的补发文案一致，改一边必须改另一边
_TIME_BANDS = [
    ('凌晨', '00:00', '06:00'),
    ('早晨', '06:30', '08:00'),
    ('早上', '07:00', '09:00'),
    ('上午', '08:00', '12:00'),
    ('中午', '12:00', '13:30'),
    ('下午', '14:00', '18:00'),
    ('傍晚', '17:30', '19:00'),
    ('晚上', '18:00', '22:00'),
    ('夜里', '21:00', '23:00'),
    ('深夜', '22:00', '23:30'),
]
_PM_BANDS = {'中午', '下午', '傍晚', '晚上', '夜里', '深夜'}
_CN_NUM = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6,
           '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12}
_BAND_WORDS = '|'.join(word for word, _, _ in _TIME_BANDS)
_TIME_TOKEN = re.compile(
    rf'^(?:(?P<band>{_BAND_WORDS}))?\s*'
    # 注意：f-string 里量词花括号必须双写（{1,2} 会被求值成元组，已踩过）
    rf'(?:(?P<hour>\d{{1,2}}|[一二两三四五六七八九十]{{1,3}})\s*[点时]'
    rf'(?:\s*(?P<half>半|(?P<mm>\d{{1,2}})分))?)?\s*$')


def _parse_time_token(value, start_side=True):
    """把 start_time/end_time 的入参解析成 HH:MM，解析不出返回 None

    接受四种形状：HH:MM（既有口径）、"下午3点"/"晚上7点半"/"6点30分"（时段词+钟点，
    午后时段 12 以下自动 +12）、"3点"/"15点"（裸钟点）、纯时段词（按
    start/end 侧取区间端点）。这是服务端兜底，不是解析器：模型按协议
    换算好 HH:MM 时走不到这些分支。
    """
    text = str(value or '').strip()
    if not text:
        return None
    m = _TIME_HHMM.match(text)
    if m and 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59:
        return f'{int(m.group(1)):02d}:{m.group(2)}'
    m = _TIME_TOKEN.match(text)
    if not m:
        return None
    band, hour_raw = m.group('band'), m.group('hour')
    if not band and not hour_raw:
        return None  # 空串以外的杂文本
    if hour_raw:
        hour = int(hour_raw) if hour_raw.isdigit() else _CN_NUM.get(hour_raw)
        if hour is None or hour > 24:
            return None
        if band in _PM_BANDS and hour < 12:
            hour += 12
        if hour > 23:
            return None
        # half 命名组捕获的是整个交替式（「30分」也算命中），判字面值
        if m.group('half') == '半':
            return f'{hour:02d}:30'
        if m.group('mm') is not None:
            minutes = int(m.group('mm'))
            if not 0 <= minutes <= 59:
                return None
            return f'{hour:02d}:{minutes:02d}'
        return f'{hour:02d}:00'
    for word, start, end in _TIME_BANDS:
        if word == band:
            return start if start_side else end
    return None


def normalize_input(data, today):
    """清洗校验解析结果（AI 与规则输出共用），丢弃非法字段"""
    out = {}
    name = str(data.get('name') or '').strip()
    if name:
        out['name'] = name[:255]
    for key in ('start_date', 'end_date'):
        value = data.get(key)
        if value:
            try:
                out[key] = date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:
                pass
    if out.get('start_date') and out.get('end_date') and out['end_date'] < out['start_date']:
        out['start_date'], out['end_date'] = out['end_date'], out['start_date']
    # 具体时间（可选，2026-09-17）：仅接受 HH:MM/H:MM；孤时间（无任何日期）丢弃
    # 2026-11-10：再加一层时段词/中文钟点兜底（_parse_time_token），模型把
    # 「晚上」「下午3点」原样写进 start_time 时不再静默丢弃
    for key in ('start_time', 'end_time'):
        value = _parse_time_token(data.get(key), start_side=(key == 'start_time'))
        if value:
            out[key] = value
    if out.get('start_time') and not out.get('start_date') and not out.get('end_date'):
        out.pop('start_time')
        out.pop('end_time', None)
    cost = data.get('cost')
    if cost is not None and cost != '':
        try:
            cost = round(float(cost), 2)
            if cost >= 0:
                out['cost'] = cost
        except (TypeError, ValueError):
            pass
    status = data.get('status')
    if status in dict(Activity.STATUS_CHOICES):
        out['status'] = status
    for key in ('tags', 'participants'):
        values = data.get(key)
        if isinstance(values, str):
            values = [v.strip() for v in re.split(r'[,，、]', values)]
        if isinstance(values, list):
            values = [str(v).strip() for v in values if str(v).strip()]
            if values:
                out[key] = values[:10]
    return out


# 活动筛选参数名（与 filter_activities 支持的键对齐；name 只给 Agent 查询工具用，不从 URL 读）
FILTER_PARAM_KEYS = ('status', 'tag', 'date_from', 'date_to', 'participant', 'keyword')


def get_filter_params(request):
    """从 URL 查询串读取活动筛选参数（列表页 / CSV / JSON 导出共用）

    只做取值 + trim，合法性（状态枚举、日期格式等）统一由 filter_activities 判定；
    排序、分页等展示类参数不属筛选，由各自视图自取。
    """
    return {key: (request.GET.get(key) or '').strip() for key in FILTER_PARAM_KEYS}


def expense_totals_map(activity_ids):
    """批量取每个活动的直接费用合计 {activity_id: Decimal}

    无费用的活动不在返回字典里，调用方用 `.get(id, 0) or 0` 兼容。
    列表页 / 导出 / attach_costs 以前各写一份同样的 groupby。
    """
    ids = list(activity_ids)
    if not ids:
        return {}
    return dict(
        Expense.objects.filter(activity_id__in=ids)
        .values_list('activity_id').annotate(total=Sum('amount'))
        .values_list('activity_id', 'total')
    )


def filter_activities(user, params):
    """按条件筛选活动，返回 queryset（列表视图与 Agent 查询工具共用）

    params 支持：status / tag / date_from / date_to / name /
    participant（参与者，模糊）/ keyword（名称/描述/参与者/标签跨字段模糊），非法值静默忽略。
    日期筛选与列表页一致：按活动开始日期是否落在区间内。
    """
    qs = visible_qs(Activity, user).prefetch_related('tags')
    status = params.get('status')
    # 支持逗号分隔多值（如 in_progress,planned）：Web Push 深链用，列表 UI 仍是单选
    if status in dict(Activity.STATUS_CHOICES):
        qs = qs.filter(status=status)
    elif status:
        valid = [s for s in status.split(',') if s in dict(Activity.STATUS_CHOICES)]
        if valid:
            qs = qs.filter(status__in=valid)
    tag = str(params.get('tag') or '').strip()
    if tag:
        qs = qs.filter(tags__name=tag)
    for key, lookup in (('date_from', 'gte'), ('date_to', 'lte')):
        value = str(params.get(key) or '').strip()[:10]
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                continue
            qs = qs.filter(**{f'start_date__{lookup}': value})
    name = str(params.get('name') or '').strip()
    if name:
        qs = qs.filter(name__icontains=name)
    participant = str(params.get('participant') or '').strip()
    if participant:
        qs = qs.filter(participants__name__icontains=participant).distinct()
    keyword = str(params.get('keyword') or '').strip()
    if keyword:
        qs = qs.filter(q_or(('name', 'description', 'tags__name',
                             'participants__name'), keyword)).distinct()
    return qs
