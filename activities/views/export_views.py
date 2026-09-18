"""CSV 数据导出：活动与费用的全量清单（数据主权兜底，可直接进 Excel）

- BOM（\ufeff）必须写：Excel 打开无 BOM 的 UTF-8 中文 CSV 全是乱码
- 只导 visible_qs 范围内的数据（超管全量、普通用户自己的），与页面口径一致
- 下载端点不加 @require_POST：GET 直链 + a[download] 是唯一消费方式
"""
import csv

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponse

from activities.models import Activity, Expense
from core.tags import tag_names
from core.utils import visible_qs


def _csv_response(filename):
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp.write('\ufeff')  # UTF-8 BOM：Excel 中文乱码的解药
    return resp


def _fmt(v):
    return '' if v is None else str(v)


@login_required
def activity_export_csv(request):
    """导出活动清单 CSV：名称/状态/起止/时间/标签/参与者/累计费用/描述"""
    resp = _csv_response('activities.csv')
    writer = csv.writer(resp)
    writer.writerow(['名称', '状态', '开始日期', '结束日期', '开始时间', '结束时间',
                     '标签', '参与者', '累计费用', '描述'])
    qs = (visible_qs(Activity, request.user)
          .prefetch_related('tags', 'participants', 'expenses')
          .order_by('-start_date', '-id'))
    for a in qs:
        total = a.expenses.aggregate(t=Sum('amount'))['t']
        writer.writerow([
            a.name,
            a.get_status_display(),
            _fmt(a.start_date),
            _fmt(a.end_date),
            _fmt(a.start_time),
            _fmt(a.end_time),
            ' '.join(tag_names(a)),
            ' '.join(p.name for p in a.participants.all()),
            _fmt(total),
            (a.description or '').replace('\r\n', '\n'),
        ])
    return resp


@login_required
def expense_export_csv(request):
    """导出费用清单 CSV：活动/金额/日期/标签/备注"""
    resp = _csv_response('expenses.csv')
    writer = csv.writer(resp)
    writer.writerow(['活动名称', '金额', '消费日期', '标签', '备注'])
    qs = (visible_qs(Expense, request.user)
          .select_related('activity')
          .prefetch_related('tags')
          .order_by('-paid_at', '-created_at'))
    for e in qs:
        writer.writerow([
            e.activity.name if e.activity else '',
            e.amount,
            _fmt(e.paid_at),
            ' '.join(t.name for t in e.tags.all()),
            e.note,
        ])
    return resp
