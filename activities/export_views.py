"""活动数据导出视图（CSV + JSON）

筛选与费用聚合全部复用列表页同一套函数（filter_activities / get_filter_params /
expense_totals_map），保证「看到的」与「导出的」口径一致。
"""
import csv
import io
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import StreamingHttpResponse, JsonResponse
from django.utils import timezone

from .utils import filter_activities, get_filter_params, expense_totals_map


def _get_filtered_activities(request):
    """复用列表页的参数读取与筛选逻辑，返回筛选后的活动 QuerySet"""
    return filter_activities(request.user, get_filter_params(request))


@login_required
def export_csv(request):
    """导出活动列表为 CSV（UTF-8 BOM，Excel 兼容）"""
    qs = _get_filtered_activities(request).prefetch_related('tags', 'participants')
    activities = list(qs)
    activity_ids = [a.id for a in activities]
    expense_totals = expense_totals_map(activity_ids)

    today_str = timezone.localdate().strftime('%Y%m%d')
    filename = f'activities_{today_str}.csv'

    # UTF-8 BOM
    bom = '\ufeff'
    header = ['ID', '名称', '状态', '开始日期', '结束日期', '标签', '参与者', '费用合计', '耗时（分钟）']

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield bom + buf.getvalue()

        for a in activities:
            tags = ','.join(a.tags.names())
            participants = ','.join(a.participants.values_list('name', flat=True))
            expense_total = float(expense_totals.get(a.id, 0) or 0)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                a.id,
                a.name,
                a.get_status_display(),
                str(a.start_date) if a.start_date else '',
                str(a.end_date) if a.end_date else '',
                tags,
                participants,
                expense_total,
                a.duration_minutes if a.duration_minutes is not None else '',
            ])
            yield buf.getvalue()

    response = StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _activity_to_dict(activity, expense_totals, children_map):
    """将活动对象序列化为字典（含嵌套子任务）"""
    tags = list(activity.tags.names()) if hasattr(activity.tags, 'names') else []
    participants = list(activity.participants.values_list('name', flat=True))
    expense_total = float(expense_totals.get(activity.id, 0) or 0)

    children = []
    for child in children_map.get(activity.id, []):
        children.append(_activity_to_dict(child, expense_totals, children_map))

    return {
        'id': activity.id,
        'name': activity.name,
        'status': activity.status,
        'start_date': str(activity.start_date) if activity.start_date else None,
        'end_date': str(activity.end_date) if activity.end_date else None,
        'tags': tags,
        'participants': participants,
        'expense_total': expense_total,
        'duration_minutes': activity.duration_minutes,
        'children': children,
    }


@login_required
def export_json(request):
    """导出活动列表为 JSON（含子任务层级）"""
    qs = _get_filtered_activities(request).prefetch_related('tags', 'participants')
    activities = list(qs)
    activity_ids = [a.id for a in activities]
    expense_totals = expense_totals_map(activity_ids)

    # 构建 children_map 用于嵌套层级
    children_map = {}
    for a in activities:
        children_map.setdefault(a.parent_id, []).append(a)

    # 只输出顶级活动（parent=None），子任务通过 children 嵌套
    top_level = [a for a in activities if a.parent_id is None]
    result = []
    for a in top_level:
        result.append(_activity_to_dict(a, expense_totals, children_map))

    return JsonResponse({
        'exported_at': datetime.now().isoformat(),
        'count': len(result),
        'activities': result,
    }, json_dumps_params={'ensure_ascii': False})
