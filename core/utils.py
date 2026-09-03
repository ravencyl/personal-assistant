from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.http import JsonResponse
from django.shortcuts import get_object_or_404


def q_or(fields, term):
    """一个词跨多列的 OR 模糊匹配（全站唯一的 Q 拼接器）

    fields 为 lookup 前缀元组，如 ('name', 'tags__name')。
    全局搜索 / 活动筛选 / 跨模块关联兜底共用，避免同一件“多列命中任一即算”
    的事在三处各拼一遍 Q（字段集合容易漏改）。
    是否分词由调用方决定（本函数只负责拼 Q）。
    """
    q = models.Q()
    for field in fields:
        q |= models.Q(**{f'{field}__icontains': term})
    return q


def visible_qs(model, user):
    """数据可见性规则：超级用户可见全部数据，普通用户仅可见自己的"""
    qs = model.objects.all()
    if user.is_superuser:
        return qs
    return qs.filter(user=user)


def get_visible(model, user, **kwargs):
    """按可见性规则取单对象，不存在或无权时 404"""
    return get_object_or_404(visible_qs(model, user), **kwargs)


def visible_child_qs(model, user, parent_lookup):
    """子模型（自身没有 user 字段）按父记录归属过滤，父记录走同一套可见性规则

    parent_lookup 是指向父模型的关系名，如 Message 用 'conversation'。
    以前这类查询在手写的 filter(xxx__user=request.user) 与 visible_qs 之间二选一，
    结果是超管能搜到别人的活动却搜不到别人会话里的消息（口径不一）。
    """
    parent_model = model._meta.get_field(parent_lookup).related_model
    return model.objects.filter(
        **{f'{parent_lookup}__in': visible_qs(parent_model, user)}
    )


def get_visible_child(model, user, parent_lookup, **kwargs):
    """visible_child_qs 的单对象版，不存在或无权时 404"""
    return get_object_or_404(visible_child_qs(model, user, parent_lookup), **kwargs)


def get_visible_or_json(model, user, message='对象不存在或已删除', **kwargs):
    """get_visible 的 JSON 端点版：不可见时返回 JsonResponse(404) 而不是抛 Http404

    返回 (obj, None) 或 (None, response)：
        template, resp = get_visible_or_json(ActivityTemplate, request.user, id=pk)
        if resp is not None:
            return resp
    存在意义是让“需要 JSON 错误体”的端点也能复用同一套可见性口径，
    而不是每个视图里手写一份 filter(...).first() + 404 JSON。
    """
    from django.http import Http404

    try:
        return get_visible(model, user, **kwargs), None
    except Http404:
        return None, JsonResponse({'error': message}, status=404)


def wants_json(request):
    """客户端是否要求 JSON（原生 fetch 通道；HTMX / 整页表单不命中）

    浏览器整页提交的 Accept 是 text/html,...，不含 application/json，不会误判。
    """
    return 'application/json' in request.headers.get('Accept', '')


def json_login_required(view_func):
    """@login_required 的 JSON 端点版：未登录时按请求类型分流，绝不给 fetch 回 302

    双协议约定下聊天/钉选这类端点由原生 fetch 消费。登录态过期时 @login_required
    的 302 会被 fetch 自动跟随，最终拿到 200 的登录页 HTML → JSON 解析失败 → 前端
    只能报「操作失败，请重试」，用户反复重试也不知道是掉线（2026-09-02 线上冒烟
    实测：anonymous POST /chat/22/pin/ 返回 302 /accounts/login/?next=/chat/22/pin/）。

    - Accept 含 application/json（fetch）→ 401 + {'error', 'login_url'}，前端读
      login_url 做一次整页跳转。**判据复用 wants_json**，与视图自己「回 JSON 还是
      回 HTML」用的是同一个口径，否则会出现「视图认为要 JSON、装饰器给了 302」
    - 其余（整页表单 / HTMX）→ 保持 Django 原生行为 302 + ?next=，不动既有交互

    登录后的路径与 @login_required 完全一致，所以本装饰器是它的超集。
    """
    from functools import wraps

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)
        # 延迟导入：django.contrib.auth.views 会带进 auth 表单链，模块加载期不必拉
        from django.contrib.auth.views import redirect_to_login
        redirect = redirect_to_login(request.get_full_path())
        if wants_json(request):
            return JsonResponse({'error': '登录已过期，请重新登录',
                                 'login_url': redirect.url,
                                 'reauth': True}, status=401)
        return redirect

    # 供测试反查「该端点是否真套上了本装饰器」（inspect 源码会被 @require_POST 等包裹干扰）
    _wrapped.json_login_required = True
    return _wrapped


def used_tags(model, qs):
    """qs 内对象上出现过的标签（Tag queryset，去重 + 按名排序）

    必须限定 content_type：taggit 的 object_id 在不同模型之间会撞号，
    只按 object_id 过滤会把别的 app 的标签混进来。
    可见范围由调用方传入的 qs 决定（本函数不改语义）。
    """
    from taggit.models import Tag

    return Tag.objects.filter(
        taggit_taggeditem_items__content_type=ContentType.objects.get_for_model(model),
        taggit_taggeditem_items__object_id__in=qs.values('id'),
    ).distinct().order_by('name')


def used_tag_names(model, qs):
    """used_tags 的标签名列表版本（表单 autocomplete / 筛选栏用）"""
    return list(used_tags(model, qs).values_list('name', flat=True))


def week_monday(d):
    """所在周的周一（ISO 周，周日记为上一周末尾）"""
    return d - timedelta(days=d.weekday())


WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
# 日历表头 / 「周X」标题用的短形式，与 WEEKDAY_LABELS 同一口径（去掉「周」前缀）
WEEKDAY_SHORT = [w[1:] for w in WEEKDAY_LABELS]


def daily_totals(rows, start_day, length=7, date_field='paid_at', value_field='amount'):
    """把带日期的记录行按天累加成固定长度序列（无记录的日期补 0）

    rows 为模型实例迭代器；适用于图表/概览的“近 N 天按日填格”，
    原先 dashboard 迷你图与费用图表各写了一遍。
    """
    buckets = [0.0] * length
    for row in rows:
        day = getattr(row, date_field, None)
        if not day:
            continue
        idx = (day - start_day).days
        if 0 <= idx < length:
            buckets[idx] += float(getattr(row, value_field, 0) or 0)
    return buckets


def pct_change(current, previous, digits=1):
    """环比百分比；无基数（previous 为 0/None）时返回 None，由调用方决定展示文案

    统一在此取整，避免同一指标在不同页面小数位数不一致。
    """
    prev = float(previous or 0)
    if prev <= 0:
        return None
    return round((float(current or 0) - prev) / prev * 100, digits)


def pending_reminders(user):
    """「待处理」提醒 —— 全站唯一定义（浮窗红点、Daily「提醒」区、AI 的 list_reminders 共用）

    口径：今天到点、且用户还没处理掉的提醒。

    必须同时查两个 status：pending→fired 的落库靠 `check_due_reminders`，而它只在
    Daily 页与对话发送时被顺手调用（没有 cron）。所以「已到点但未触发落库的 pending」
    与「已触发未处理的 fired」是同一件事的两种存储形态，只查其中一个会让结果
    取决于用户先打开过哪个页面：曾经红点按两者之和计、Daily 列表只取 fired、
    AI 工具只取字面 pending，三处互相矛盾（红点亮着 1、页面空白、问 AI 答「没有」）。

    两侧都限定 trigger_at 在今天：
    - fired 不限今天的话，几年前没处理的老提醒会挂住一个消不掉的红点
    - pending 不限今天的话，昨天的过期提醒会计入红点，但 Daily 列表（只看今天）
      里没有它，用户点进来看不到任何东西
    两种情况都只能靠页面上的「已完成 / 忽略」按钮出口消掉，不自动过期。

    done/dismissed 属于已处理，任何一天都不再出现。
    """
    from django.utils import timezone

    from core.models import Reminder

    now = timezone.now()
    today = timezone.localdate()
    return Reminder.objects.filter(user=user).filter(
        models.Q(status='pending', trigger_at__lte=now, trigger_at__date=today)
        | models.Q(status='fired', trigger_at__date=today)
    ).order_by('trigger_at')


def upcoming_reminders(user, until=None):
    """「待触发」提醒 —— 今天还没到点的那些（Daily 右列的今日预告）

    与 pending_reminders 严格互斥（下界是 now）：同一条提醒不会既出现在左列「提醒」区
    又出现在右列「待触发提醒」。以前右列自己按 status='pending' 取，没跑过
    check_due_reminders 时已到点的提醒会同时出现在两处。

    until 给定时取到该时刻为止（建议规则想拿「接下来几小时」的可以用）。
    """
    from django.utils import timezone

    from core.models import Reminder

    now = timezone.now()
    if until is None:
        tomorrow = timezone.localdate() + timedelta(days=1)
        until = timezone.make_aware(
            timezone.datetime.combine(tomorrow, timezone.datetime.min.time()))
    return Reminder.objects.filter(
        user=user, status='pending', trigger_at__gte=now, trigger_at__lt=until,
    ).order_by('trigger_at')


def char_overlap_ratio(a, b, mode='symmetric'):
    """字符重叠率（全站唯一的「两段文本像不像」实现）

    mode='symmetric'：交集大小 / 两者较大的字符集，衡量双向相似度（建议行的目标进展匹配用）。
    mode='contains' ：a 中出现在 b 里的字符数 / len(a)，衡量 a 是否已被 b 覆盖（记忆查重）。

    两种口径此前分别写在 core/suggestions 与 memory/services 里，语义并不等价（双向 vs 单向），
    因此收敛为一个函数的两个 mode，而不是强行统一——那会悄悄改变判定结果。
    """
    if not a or not b:
        return 0.0
    if mode == 'contains':
        return sum(1 for c in a if c in b) / len(a)
    set_a, set_b = set(a), set(b)
    return len(set_a & set_b) / max(len(set_a), len(set_b), 1)
