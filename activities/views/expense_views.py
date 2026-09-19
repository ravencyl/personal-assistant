"""费用端点：记一笔 / 编辑 / 删除 / 报告页 / 图表数据"""
import re
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.db.models import Count, Sum

from core.tags import apply_tags, tag_names
from core.utils import get_visible, visible_qs, week_monday, pct_change, daily_totals, WEEKDAY_LABELS

from ..models import Activity, Expense
from ..services import (InputError, add_expense, clean_amount, clean_paid_at)
from ..utils import daily_bucket_q, get_daily_bucket, log_activity


@login_required
@require_POST
def expense_create(request, activity_id):
    """为活动添加费用条目"""
    activity = get_visible(Activity, request.user, id=activity_id)
    try:
        # 空金额不再静默建 0 元记录（与快记入口同一口径）
        expense = add_expense(
            activity, request.user,
            request.POST.get('amount'),
            paid_at=request.POST.get('paid_at'),
            note=request.POST.get('note'),
            positive=True,
            tags=request.POST.get('tags'),
        )
        if expense is None:
            raise InputError('金额不能为空')
    except InputError as e:
        return JsonResponse({'error': str(e)}, status=400)
    log_activity(request.user, activity, 'edited',
                 f'添加费用 ¥{expense.amount}'
                 + (f' {expense.note}' if expense.note else ''))

    if request.headers.get('HX-Request') or request.headers.get('Accept') == 'application/json':
        agg = activity.expenses.aggregate(total=Sum('amount'), cnt=Count('id'))
        return JsonResponse({
            'id': expense.id,
            'amount': float(expense.amount),
            'tags': ', '.join(tag_names(expense)),
            'note': expense.note,
            'paid_at': expense.paid_at,
            'expense_total': float(agg['total'] or 0),
            'expense_count': agg['cnt'] or 0,
        })
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_GET
def expense_quick_candidates(request):
    """快记费用的归属候选：所有「计划」和「进行中」的未归档活动（JSON 端点）

    供快记面板提交金额后的选择卡消费；「日常开支」桶不进列表——它由前端
    作为独立的兑底按钮提供，不与真实活动混在一起。q 参数过滤名称。
    """
    qs = (visible_qs(Activity, request.user)
          .filter(status__in=['planned', 'in_progress'], archived_at__isnull=True)
          .exclude(daily_bucket_q()))
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(name__icontains=q)
    items = list(qs[:100])
    # 进行中优先，同级按最近更新；个人活动量级下 Python 排序足够
    items.sort(key=lambda a: (a.status != 'in_progress', -a.updated_at.timestamp()))
    labels = dict(Activity.STATUS_CHOICES)
    return JsonResponse({'success': True, 'items': [
        {'id': a.id, 'name': a.name, 'status': a.status,
         'status_label': labels.get(a.status, a.status)}
        for a in items]})


@login_required
@require_POST
def expense_quick_create(request):
    """全局快记：一键记一笔费用（JSON 端点，由原生 fetch 消费）

    活动 id 可选；缺省记入「日常开支」归属桶。快记面板的两步流会先让
    用户从候选里选归属（expense_quick_candidates），点选后带着 id 提交；
    直接入桶只剩「日常开支」兑底按钮一条路。校验逻辑与 expense_create 一致。
    """
    activity_id = (request.POST.get('activity_id') or '').strip()
    if activity_id:
        activity = visible_qs(Activity, request.user).filter(id=activity_id).first()
        if not activity:
            return JsonResponse({'error': '活动不存在或无权访问'}, status=400)
    else:
        activity = get_daily_bucket(request.user)

    try:
        expense = add_expense(
            activity, request.user,
            request.POST.get('amount'),
            paid_at=request.POST.get('paid_at'),
            note=request.POST.get('note'),
            positive=True,
            tags=request.POST.get('tags'),
        )
        if expense is None:
            raise InputError('费用金额不能为空')
    except InputError as e:
        return JsonResponse({'error': str(e)}, status=400)
    log_activity(request.user, activity, 'edited',
                 f'快记费用 ¥{expense.amount}'
                 + (f' {expense.note}' if expense.note else ''))

    agg = activity.expenses.aggregate(total=Sum('amount'), cnt=Count('id'))
    return JsonResponse({
        'success': True,
        'amount': float(expense.amount),
        'tags': ', '.join(tag_names(expense)),
        'activity_name': activity.name,
        'expense_total': float(agg['total'] or 0),
        'expense_count': agg['cnt'] or 0,
    })


@login_required
@require_POST
def expense_edit(request, expense_id):
    """编辑费用条目"""
    expense = get_visible(Expense, request.user, id=expense_id)
    activity = expense.activity
    try:
        amount = clean_amount(request.POST.get('amount'), label='金额', required=True)
    except InputError as e:
        return JsonResponse({'error': str(e)}, status=400)

    expense.amount = amount
    # 日期被清空就真清空（invalid=None），不静默回落今天
    expense.paid_at = clean_paid_at(request.POST.get('paid_at'), invalid=None)
    expense.note = request.POST.get('note', '').strip()[:255]
    expense.save()
    apply_tags(expense, request.POST.get('tags'))
    log_activity(request.user, activity, 'edited',
                 f'编辑费用 ¥{amount}'
                 + (f' {expense.note}' if expense.note else ''))

    if request.headers.get('HX-Request') or request.headers.get('Accept') == 'application/json':
        return JsonResponse({
            'id': expense.id,
            'amount': float(expense.amount),
            'note': expense.note,
            'paid_at': expense.paid_at,
            'tags': tag_names(expense),
        })
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def expense_delete(request, expense_id):
    """删除费用条目"""
    expense = get_visible(Expense, request.user, id=expense_id)
    activity = expense.activity
    note_desc = f'¥{expense.amount}'
    if expense.note:
        note_desc += f' {expense.note}'
    expense.delete()
    log_activity(request.user, activity, 'edited', f'删除费用 {note_desc}')

    if request.headers.get('HX-Request') or request.headers.get('Accept') == 'application/json':
        return JsonResponse({'ok': True})
    return redirect('activities:activity_detail', activity.id)


@login_required
def expense_report(request):
    """费用报告页面"""
    today = timezone.localdate()

    # 本月费用合计
    month_start = today.replace(day=1)
    this_month_total = Expense.objects.filter(
        user=request.user, paid_at__gte=month_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    # 上月费用合计
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    last_month_total = Expense.objects.filter(
        user=request.user, paid_at__gte=last_month_start, paid_at__lt=month_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    # 本周费用合计
    week_start = week_monday(today)
    this_week_total = Expense.objects.filter(
        user=request.user, paid_at__gte=week_start
    ).aggregate(s=Sum('amount'))['s'] or 0

    this_month_f = float(this_month_total)
    last_month_f = float(last_month_total)

    return render(request, 'activities/expense_report.html', {
        'this_month_total': this_month_f,
        'last_month_total': float(last_month_total),
        'this_week_total': float(this_week_total),
        'month_change': pct_change(this_month_f, last_month_f),
    })


@login_required
def expense_chart_data(request):
    """费用图表数据 API"""
    from django.db.models.functions import TruncMonth

    today = timezone.localdate()
    range_type = request.GET.get('range', 'month')  # month / week / tag / month_tag

    qs = Expense.objects.filter(user=request.user)

    if range_type == 'month':
        # 近 6 个月月度趋势
        six_months_ago = today - timedelta(days=180)
        data = list(
            qs.filter(paid_at__gte=six_months_ago)
            .annotate(month=TruncMonth('paid_at'))
            .values('month')
            .annotate(total=Sum('amount'))
            .order_by('month')
        )
        return JsonResponse({
            'labels': [d['month'].strftime('%Y-%m') for d in data],
            'values': [float(d['total']) for d in data],
        })

    elif range_type == 'week':
        # 本周 vs 上周每日对比
        this_week_start = week_monday(today)
        last_week_start = this_week_start - timedelta(days=7)

        this_week = qs.filter(paid_at__gte=this_week_start, paid_at__lte=today)
        last_week = qs.filter(paid_at__gte=last_week_start, paid_at__lt=this_week_start)

        this_data = daily_totals(this_week, this_week_start)
        last_data = daily_totals(last_week, last_week_start)

        return JsonResponse({
            'labels': WEEKDAY_LABELS,
            'this_week': this_data,
            'last_week': last_data,
        })

    elif range_type == 'tag':
        # 标签饼图（近 12 个月；一笔费用可多标签，各标签独立计入金额）
        year_ago = today - timedelta(days=365)
        data = list(
            qs.filter(paid_at__gte=year_ago)
            .values('tags__name')
            .annotate(total=Sum('amount'))
            .order_by('-total')
            .exclude(tags__name__isnull=True)
        )
        return JsonResponse({
            'labels': [d['tags__name'] for d in data],
            'values': [float(d['total']) for d in data],
        })

    elif range_type == 'month_tag':
        # 单月标签明细（month 参数 YYYY-MM，缺省/非法回退当月）
        m = re.match(r'^(\d{4})-(\d{1,2})$', (request.GET.get('month') or '').strip())
        if m and 1 <= int(m.group(2)) <= 12:
            year, month = int(m.group(1)), int(m.group(2))
        else:
            year, month = today.year, today.month
        data = list(
            qs.filter(paid_at__year=year, paid_at__month=month)
            .values('tags__name')
            .annotate(total=Sum('amount'))
            .order_by('-total')
            .exclude(tags__name__isnull=True)
        )
        grand_total = sum(float(d['total']) for d in data)
        items = [{
            'tag': d['tags__name'],
            'amount': float(d['total']),
            'pct': round(float(d['total']) * 100 / grand_total, 1) if grand_total else 0,
        } for d in data]
        return JsonResponse({
            'month': f'{year:04d}-{month:02d}',
            'total': grand_total,
            'items': items,
        })

    return JsonResponse({'labels': [], 'values': []})


@login_required
def expense_heatmap_data(request):
    """费用热力图数据 API：最近 12 个月 x 费用类别的聚合金额

    返回 {months: ['YYYY-MM', ...], categories: ['cat1', ...], data: [[amount, ...]]}
    data[row][col] = 该月该类别的总金额，无数据为 0。"""
    from django.db.models.functions import TruncMonth

    today = timezone.localdate()
    # 最近 12 个月
    months = []
    for i in range(11, -1, -1):
        d = today.replace(day=1) - timedelta(days=i * 28)
        d = d.replace(day=1)
        label = d.strftime('%Y-%m')
        if label not in months:
            months.append(label)
    # 确保正好 12 个
    months = months[-12:]

    qs = Expense.objects.filter(user=request.user)

    # 获取所有费用类别（从标签中提取）
    all_tags = list(
        qs.values_list('tags__name', flat=True)
        .exclude(tags__name__isnull=True)
        .distinct()
    )[:10]  # 最多 10 个类别

    if not all_tags:
        return JsonResponse({'months': months, 'categories': [], 'data': []})

    # 查询每月每标签的聚合金额
    year_ago = today - timedelta(days=365)
    tag_month_data = list(
        qs.filter(paid_at__gte=year_ago)
        .annotate(month=TruncMonth('paid_at'))
        .values('month', 'tags__name')
        .annotate(total=Sum('amount'))
        .order_by('month')
        .exclude(tags__name__isnull=True)
    )

    # 构建矩阵
    matrix = {tag: {m: 0.0 for m in months} for tag in all_tags}
    for row in tag_month_data:
        tag = row['tags__name']
        if tag not in matrix:
            continue
        month_label = row['month'].strftime('%Y-%m')
        if month_label in matrix[tag]:
            matrix[tag][month_label] = float(row['total'])

    data = []
    for tag in all_tags:
        data.append([matrix[tag][m] for m in months])

    return JsonResponse({
        'months': months,
        'categories': all_tags,
        'data': data,
    })
