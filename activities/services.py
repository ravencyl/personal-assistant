"""活动写路径服务层

四份「创建活动」与四份「记费用」的公共实现：视图与 Agent 工具只负责取参、鉴权、
渲染，字段清洗 → 建对象 → 记费用 → 打标签 → 解析参与者 → 写日志 的顺序只保留这一份。

收敛前各路径口径不同（金额有的走 float 有的走 Decimal、非法日期三种回落方式、
空金额一处建 0 元费用一处返回 400、子任务费用一处记在父活动一处记在子任务），
这类差异正是「同一件事在不同入口结果不一样」的来源。
"""
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Activity, Expense
from .utils import log_activity, resolve_participants

logger = logging.getLogger(__name__)

# 「已花的钱」统一落这个类别；解析结果里没有类别概念
PARSED_EXPENSE_CATEGORY = 'other'

# add_expense 的 paid_at 未传占位（不能用 None，本模块里 None = 主动清空日期）
_UNSET = object()


class InputError(Exception):
    """写输入校验失败

    视图捕获后转 400 + 友好文案；Agent 工具转 ToolError（编排器再转 ⚠️ 提示）。
    message 面向用户，不得含堆栈或内部字段名。
    """


def clean_amount(raw, *, label='金额', positive=False, required=False):
    """金额输入 → Decimal(两位) | None

    - 空值：required=True 时报错，否则返回 None（调用方决定「不记这笔」还是「清空」）
    - 非数字 / 负数 / （positive 时）零值 → InputError
    用字符串构造 Decimal，避免 float 二进制的 0.0000000001 尾差写进库。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise InputError(f'{label}不能为空')
        return None
    try:
        amount = Decimal(str(raw).strip()).quantize(Decimal('0.01'))
    except (TypeError, ValueError, InvalidOperation):
        raise InputError(f'{label}格式不正确，请输入数字')
    if amount < 0:
        raise InputError(f'{label}不能为负数')
    if positive and amount == 0:
        raise InputError(f'{label}必须大于 0')
    return amount


def clean_category(raw, *, default=PARSED_EXPENSE_CATEGORY):
    """费用类别 → 合法取值（英文 key 或中文显示名），识别不了静默落 default

    中文→key 的映射直接由 Expense.CATEGORY_CHOICES 反查得出，不另存一份别名表，
    否则新增类别时这里会漏同步。
    """
    text = str(raw or '').strip()
    if not text:
        return default
    choices = dict(Expense.CATEGORY_CHOICES)
    if text in choices:
        return text
    return _CATEGORY_BY_LABEL.get(text, default)


# 中文显示名 → key（CATEGORY_CHOICES 的反查）
_CATEGORY_BY_LABEL = {label: key for key, label in dict(Expense.CATEGORY_CHOICES).items()}


def clean_paid_at(raw, *, invalid='today'):
    """费用日期 → date

    invalid 决定非法/空值的语义：'today' 回落到今天（记一笔的默认场景），
    None 表示清空（编辑费用时用户删掉日期）。
    """
    text = str(raw or '').strip()
    if not text:
        return timezone.localdate() if invalid == 'today' else None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return timezone.localdate() if invalid == 'today' else None


def add_expense(activity, user, raw_amount, *, category=None, paid_at=_UNSET,
                note='', positive=False, clear_date=False):
    """为活动记一笔支出（所有「记一笔」入口的唯一写库处）

    金额：空值返回 None（表示「这次没花钱」，不建 0 元记录）；非法金额抛 InputError；
    positive=True 时 0 也视为非法（用于「记一笔」入口：0 元记录没有意义）。
    日期：收敛前三个入口三套回落（空→今天 / 非法→今天 / 一律留空），现在只有两条规则：
    - 创建场景：未传 / 空 / 非法 → 今天（“不填就是现在”，与输入框提示同口径）
    - clear_date=True → 强制留空，只给派生记录（AA 分账拆出的多笔不等于今天花的钱）
    编辑费用时“删掉日期”不走本函数，由视图直接调 clean_paid_at(invalid=None)。
    """
    amount = clean_amount(raw_amount, label='费用金额', positive=positive)
    if amount is None:
        return None
    paid_at_value = None if clear_date else clean_paid_at(
        None if paid_at is _UNSET else paid_at)
    expense = Expense.objects.create(
        activity=activity,
        user=user,
        amount=amount,
        category=clean_category(category),
        paid_at=paid_at_value,
        note=str(note or '').strip()[:255],
    )
    return expense


def record_parsed_cost(activity, user, cost, *, note='快速输入创建'):
    """把一句话解析出来的花费记为该活动的一笔支出

    语义是「已经花掉的钱」，与 Activity.budget（预算上限）不是一回事，不得写进 budget。
    空值 / 0 / 非法金额均返回 None：解析结果里的花费是附加信息，不能因为它
    不合法就阻断活动创建，故吞掉 InputError 只记日志；0 元视作「本次没花钱」。
    """
    if cost in (None, ''):
        return None
    try:
        amount = clean_amount(cost, label='费用金额')
    except InputError as e:
        logger.warning(f'解析费用未记入 activity={activity.id}: {e}')
        return None
    if not amount:
        return None
    return add_expense(activity, user, amount, note=note)


def create_activity_from_parsed(user, data, *, parent=None, source='',
                                create_missing_participants=False, cost_note=None):
    """按已 normalize 的解析结果创建活动（快速创建 / 快速子任务 / Agent 工具共用）

    参数：
    - data: `utils.normalize_input` 的输出（字段已清洗，日期是 ISO 字符串）
    - parent: 传入则创建为子活动，归属继承父活动（AGENTS.md：子活动 user 不变）
    - source: 日志与提示文案里的来源说明，如「快速输入」「AI 对话」
    - create_missing_participants: 自动识别路径 False（只匹配已有联系人），
      用户显式填写路径 True
    - cost_note: 费用备注；缺省时子任务用「子任务「x」费用」，顶级用「{source}创建」

    返回 dict：{activity, expense, skipped, created}。名称非法由调用方提前挡住
    （normalize_input 已丢弃非法字段），本函数不抛 InputError。
    """
    owner = parent.user if parent else user
    activity = Activity.objects.create(
        user=owner,
        name=data['name'],
        parent=parent,
        start_date=data.get('start_date'),
        end_date=data.get('end_date'),
        status=data.get('status', 'planned'),
        budget=data.get('budget'),
        duration_minutes=data.get('duration_minutes'),
    )

    if cost_note is None:
        cost_note = (f'子任务「{activity.name}」费用' if parent
                     else f'{source}创建' if source else '快速输入创建')
    expense = record_parsed_cost(activity, owner, data.get('cost'), note=cost_note)

    if data.get('tags'):
        activity.tags.add(*data['tags'])

    skipped, created = [], []
    if data.get('participants'):
        participants, skipped, created = resolve_participants(
            owner, data['participants'], create_missing=create_missing_participants)
        if participants:
            activity.participants.set(participants)

    if parent:
        log_activity(user, parent, 'sub_created', f'创建子任务「{activity.name}」')
    log_activity(user, activity, 'created',
                 (f'在父活动「{parent.name}」下创建' if parent
                  else f'通过{source}创建' if source else '创建'))
    return {'activity': activity, 'expense': expense,
            'skipped': skipped, 'created': created}


def due_activities(user=None):
    """到期该转为进行中的活动集（开始日期已到且状态仍为 planned）

    单一判定：以前 cron 命令与视图各写一份 filter，--dry-run 可能与真实行为漂移。
    """
    qs = Activity.objects.filter(
        status='planned',
        start_date__lte=timezone.localdate(),
        start_date__isnull=False,
    )
    return qs.filter(user=user) if user is not None else qs


def start_due_activities(user=None, *, dry_run=False):
    """把到期活动置为进行中，返回受影响的活动列表（未变更时为空列表）

    cron（`auto_start_activities`）与页面访问（activity_list / activity_detail）共用本函数，
    访问即同步是有意保留的：新建一个「今天开始」的活动后，不等到下一个 30 分钟的
    cron 就能在 Daily 上看到「进行中」。dry_run=True 只查不写，与真实执行同一口径。
    每条变更写一条日志，操作人固定为 system（该用户名不存在则跳过日志，状态变更照常）。
    """
    activities = list(due_activities(user))
    if dry_run or not activities:
        return activities

    system_user = get_user_model().objects.filter(username='system').first()
    for activity in activities:
        activity.status = 'in_progress'
        activity.save(update_fields=['status', 'updated_at'])
        if system_user:
            # log_activity 内部已 try/except，写失败仅告警
            log_activity(
                system_user, activity, 'status_changed',
                f'系统自动调整：计划→进行中（开始日期 {activity.start_date} 已到）')
        else:
            logger.warning('未找到 username=system 用户，自动状态变更未写日志'
                           f' activity={activity.id}')
    logger.info(f'自动状态变更: {len(activities)} 个活动 planned → in_progress')
    return activities
