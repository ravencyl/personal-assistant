from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404


def visible_qs(model, user):
    """数据可见性规则：超级用户可见全部数据，普通用户仅可见自己的"""
    qs = model.objects.all()
    if user.is_superuser:
        return qs
    return qs.filter(user=user)


def get_visible(model, user, **kwargs):
    """按可见性规则取单对象，不存在或无权时 404"""
    return get_object_or_404(visible_qs(model, user), **kwargs)


def wants_json(request):
    """客户端是否要求 JSON（原生 fetch 通道；HTMX / 整页表单不命中）

    浏览器整页提交的 Accept 是 text/html,...，不含 application/json，不会误判。
    """
    return 'application/json' in request.headers.get('Accept', '')


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
