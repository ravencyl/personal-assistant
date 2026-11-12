"""活动写路径服务层

四份「创建活动」与四份「记费用」的公共实现：视图与 Agent 工具只负责取参、鉴权、
渲染，字段清洗 → 建对象 → 记费用 → 打标签 → 解析参与者 → 写日志 的顺序只保留这一份。

收敛前各路径口径不同（金额有的走 float 有的走 Decimal、非法日期三种回落方式、
空金额一处建 0 元费用一处返回 400、活动安排费用一处记在父活动一处记在活动安排），
这类差异正是「同一件事在不同入口结果不一样」的来源。
"""
import logging
import re
import threading
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from core.notifications import notify
from core.tags import apply_tags

from .models import Activity, ActivityDescriptionTask, Expense
from .utils import edit_summary, log_activity, resolve_participants, snapshot_activity

logger = logging.getLogger(__name__)

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


def add_expense(activity, user, raw_amount, *, paid_at=_UNSET,
                note='', positive=False, clear_date=False, tags=None):
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
        paid_at=paid_at_value,
        note=str(note or '').strip()[:255],
    )
    if tags:
        # 费用标签（scope='expense'）：与活动标签同表隔离，AI 记账/详情页共用
        apply_tags(expense, tags)
    return expense


def record_parsed_cost(activity, user, cost, *, note='快速输入创建'):
    """把一句话解析出来的花费记为该活动的一笔支出

    语义是「已经花掉的钱」。空值 / 0 / 非法金额均返回 None：解析结果里的花费是附加信息，
    不能因为它不合法就阻断活动创建，故吞掉 InputError 只记日志；0 元视作「本次没花钱」。
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
    """按已 normalize 的解析结果创建活动（快速创建 / 快速活动安排 / Agent 工具共用）

    参数：
    - data: `utils.normalize_input` 的输出（字段已清洗，日期是 ISO 字符串）
    - parent: 传入则创建为子活动，归属继承父活动（AGENTS.md：子活动 user 不变）
    - source: 日志与提示文案里的来源说明，如「快速输入」「AI 对话」
    - create_missing_participants: 自动识别路径 False（只匹配已有联系人），
      用户显式填写路径 True
    - cost_note: 费用备注；缺省时活动安排用「活动安排「x」费用」，顶级用「{source}创建」

    返回 dict：{activity, expense, skipped, created}。名称非法由调用方提前挡住
    （normalize_input 已丢弃非法字段），本函数不抛 InputError。
    """
    owner = parent.user if parent else user
    activity = Activity.objects.create(
        user=owner,
        name=data['name'],
        parent=parent,
        description=data.get('description') or '',
        start_date=data.get('start_date'),
        end_date=data.get('end_date'),
        start_time=data.get('start_time'),
        end_time=data.get('end_time'),
        status=data.get('status', 'planned'),
    )

    if cost_note is None:
        cost_note = (f'活动安排「{activity.name}」费用' if parent
                     else f'{source}创建' if source else '快速输入创建')
    expense = record_parsed_cost(activity, owner, data.get('cost'), note=cost_note)

    if data.get('tags'):
        apply_tags(activity, data['tags'])

    skipped, created = [], []
    if data.get('participants'):
        participants, skipped, created = resolve_participants(
            owner, data['participants'], create_missing=create_missing_participants)
        if participants:
            activity.participants.set(participants)

    if parent:
        log_activity(user, parent, 'sub_created', f'创建活动安排「{activity.name}」')
    log_activity(user, activity, 'created',
                 (f'在父活动「{parent.name}」下创建' if parent
                  else f'通过{source}创建' if source else '创建'))
    return {'activity': activity, 'expense': expense,
            'skipped': skipped, 'created': created}


def change_activity_status(activity, user, status, *, via=''):
    """状态变更的统一写库处（页面快捷按钮 / AI 单改 / AI 批量三入口共用）

    调用方负责保证 status 合法且 != activity.status；本函数只做落库 + 日志，
    返回 (old_label, new_label) 供调用方拼提示文案。via 为来源后缀，只进日志。
    """
    labels = dict(Activity.STATUS_CHOICES)
    old_label = labels.get(activity.status, activity.status)
    activity.status = status
    activity.save(update_fields=['status', 'updated_at'])
    new_label = labels.get(status, status)
    log_activity(user, activity, 'status_changed',
                 f'状态「{old_label}」→「{new_label}」' + (f'（{via}）' if via else ''))
    return old_label, new_label


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


# ── 活动描述 AI 优化（2026-11-12）─────────────────────────────────────────────
# 一键优化 → 后台 daemon 线程跑 ai_round_trip → 写回描述 + 落通知。优化期间描述
# 锁定不可编辑（每活动至多一个 running 任务，partial unique 约束在数据库层挡竞态）。
# 前端轮询状态端点；线程因 worker 重启/崩溃留下僵尸 running 时按 updated_at 超时回收。

DESC_OPTIMIZE_TIMEOUT = 90        # 单次 AI 往返上限（与聊天 AI_WAIT_TIMEOUT 同量级）
DESC_OPTIMIZE_STALE_SECONDS = 240  # running 超过此时长视为线程已死，回收解锁（> 90s 留足余量）


class OptimizeBusyError(InputError):
    """该活动已有优化在跑（视图转 409 + optimizing 标记，前端据此恢复轮询）。"""


def build_optimization_prompt(description, activity_name=''):
    """描述优化的提示词：梳理逻辑 / 用词 / 错别字 / 语病四项都点到，且守住「不增删事实」。"""
    return (
        '你是中文文字编辑。请优化下面这条活动描述的文字表达：梳理文字逻辑与顺序、'
        '修正错别字、改正语病、让用词更准确自然。\n'
        '要求：\n'
        '- 保持原意，不新增事实、不删减关键信息（时间、地点、人物、金额、待办等必须保留）。\n'
        '- 保持第一人称与原有语气，不要改成报告体或公文腔。\n'
        '- 保留原有的分段与列表结构（如果有）。\n'
        '- 只输出优化后的描述正文，不要任何解释、标题、前后缀或代码块。\n\n'
        f'活动名称：{activity_name}\n'
        f'原始描述：\n{description}'
    )


def clean_optimized_text(text):
    """从 AI 回复里取出描述正文：剥外层代码块与「优化后的描述：」这类前缀，保留分段。

    提示词已要求只输出正文，这里只做保守兜底，绝不二次改写用户内容。空/None → ''。
    """
    if not text:
        return ''
    s = str(text).strip()
    if s.startswith('```'):
        s = re.sub(r'^```[a-zA-Z]*\s*\n?', '', s)
        s = re.sub(r'\n?```\s*$', '', s).strip()
    s = re.sub(r'^(?:优化后的?描述|优化结果|修改后的?描述)\s*[：:]\s*', '', s).strip()
    return s


def active_description_task(activity):
    """返回该活动当前 running 的优化任务；没有则 None。顺带回收僵尸 running。

    僵尸回收：running 但 updated_at 超过 DESC_OPTIMIZE_STALE_SECONDS（线程随 worker
    重启/崩溃死了，永远不会自己收尾）→ 置 error + 发失败通知 + 解锁。合法任务在 90s
    内必收尾，240s 阈值不会误伤正在跑的任务。
    """
    task = (ActivityDescriptionTask.objects
            .filter(activity=activity, status='running')
            .select_related('user').first())
    if task is None:
        return None
    age = (timezone.now() - task.updated_at).total_seconds()
    if age > DESC_OPTIMIZE_STALE_SECONDS:
        task.status = 'error'
        task.error = '优化超时，已自动取消'
        task.save(update_fields=['status', 'error', 'updated_at'])
        notify(task.user, f'「{activity.name}」描述优化超时',
               kind='desc_optimize_failed',
               body='优化耗时过长已自动取消，描述保持原样，可重新发起。',
               link=reverse('activities:activity_detail', args=[activity.id]))
        logger.warning(f'描述优化僵尸任务回收 task={task.id} activity={activity.id} age={age:.0f}s')
        return None
    return task


def start_description_optimization(activity, user):
    """发起一次描述优化：校验 → 建 running 任务 → 起后台线程。返回任务对象。

    - 描述为空 → InputError（没东西可优化，视图转 400）。
    - 已有 running（含并发竞态撞 partial unique）→ OptimizeBusyError（视图转 409）。
    线程入口 _spawn_optimization 单独抽出，便于测试替身（lock 测试 no-op / worker 测试同步跑）。
    """
    if not (activity.description or '').strip():
        raise InputError('描述为空，先写点内容再优化')
    if active_description_task(activity) is not None:
        raise OptimizeBusyError('描述正在优化中，请等完成后再试')
    try:
        # 包一层 savepoint：撞 partial unique 抛 IntegrityError 时只回滚这个保存点，
        # 不把外层事务（测试的 atomic / 请求的事务）打成「已中止」状态。
        with transaction.atomic():
            task = ActivityDescriptionTask.objects.create(
                activity=activity, user=user,
                status='running', original=activity.description,
            )
    except IntegrityError:
        # 先读后写的窗口里另一请求刚建了 running：数据库 partial unique 兜底
        raise OptimizeBusyError('描述正在优化中，请等完成后再试')
    _spawn_optimization(task.id)
    return task


def _spawn_optimization(task_id):
    """起 daemon 线程跑优化（测试替身点：lock 测试 patch 成 no-op，worker 测试 patch 成同步）。"""
    threading.Thread(
        target=run_description_optimization, args=(task_id,),
        daemon=True, name=f'desc-opt-{task_id}',
    ).start()


def run_description_optimization(task_id):
    """后台 worker：调 AI → 写回描述 + 日志 → 任务 done + 成功通知；失败置 error + 失败通知。

    线程独立 DB 连接，finally 必须 connection.close()（gthread 下不关会泄漏连接）。
    写回前 refresh 任务确认仍 running（可能已被僵尸回收），不是则放弃，绝不覆盖。
    """
    from core.ai import ai_round_trip

    task = (ActivityDescriptionTask.objects.filter(id=task_id)
            .select_related('activity', 'user').first())
    if task is None or task.status != 'running':
        return
    activity, user = task.activity, task.user
    detail_url = reverse('activities:activity_detail', args=[activity.id])
    try:
        prompt = build_optimization_prompt(task.original, activity.name)
        raw = ai_round_trip(prompt, timeout=DESC_OPTIMIZE_TIMEOUT, purpose='content')
        task.refresh_from_db()
        if task.status != 'running':
            return  # 已被回收/取消，放弃写回
        cleaned = clean_optimized_text(raw)
        if not cleaned:
            raise ValueError('AI 未返回有效内容')
        activity.refresh_from_db()
        old = snapshot_activity(activity)
        activity.description = cleaned
        activity.save(update_fields=['description', 'updated_at'])
        log_activity(user, activity, 'edited', 'AI 优化：' + edit_summary(old, activity))
        task.result = cleaned
        task.status = 'done'
        task.save(update_fields=['result', 'status', 'updated_at'])
        notify(user, f'「{activity.name}」描述已优化',
               kind='desc_optimized',
               body='AI 已梳理文字逻辑、用词与错别字语病，点击查看优化后的描述。',
               link=detail_url)
    except Exception as e:
        logger.warning(f'描述优化失败 task={task_id} activity={activity.id}: {e}')
        try:
            task.refresh_from_db()
            if task.status == 'running':
                task.status = 'error'
                task.error = '优化失败，请稍后重试'
                task.save(update_fields=['status', 'error', 'updated_at'])
                notify(user, f'「{activity.name}」描述优化失败',
                       kind='desc_optimize_failed',
                       body='本次优化没有成功，描述保持原样，可稍后重新发起。',
                       link=detail_url)
        except Exception as inner:  # 收尾失败也只告警，线程必须正常退出
            logger.warning(f'描述优化失败收尾异常 task={task_id}: {inner}')
    finally:
        connection.close()
