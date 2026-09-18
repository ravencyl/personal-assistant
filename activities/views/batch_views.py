"""批量操作：多选活动后批量改状态 / 改标签"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone

from core.utils import visible_qs
from ..models import Activity

logger = logging.getLogger(__name__)


@login_required
def batch_update_view(request):
    """批量更新活动（POST JSON: {ids: [], changes: {status?, tags?, date?}}）"""
    if request.method != 'POST':
        return JsonResponse({'error': '仅支持 POST'}, status=405)

    try:
        data = json.loads(request.body)
        ids = data.get('ids', [])
        changes = data.get('changes', {})
    except (ValueError, TypeError):
        return JsonResponse({'error': '无效的请求数据'}, status=400)

    if not ids or not changes:
        return JsonResponse({'error': '缺少 ids 或 changes'}, status=400)

    activities = visible_qs(Activity, request.user).filter(pk__in=ids)
    updated = 0
    update_fields = []

    if 'status' in changes and changes['status'] in dict(Activity.STATUS_CHOICES):
        activities.update(status=changes['status'])
        update_fields.append('status')

    if 'date' in changes:
        from datetime import date as date_type
        try:
            new_date = date_type.fromisoformat(changes['date'])
            activities.update(start_date=new_date)
            update_fields.append('date')
        except (ValueError, TypeError):
            pass

    if 'tags' in changes:
        from core.tags import tag_names
        tag_list = tag_names(changes['tags'])
        for act in activities:
            act.tags.set(tag_list)
        update_fields.append('tags')

    # 记录操作日志
    try:
        from activities.views._common import log_activity
        for act in activities:
            log_activity(request.user, act, 'batch_update',
                         f'批量更新: {", ".join(update_fields)}')
    except Exception:
        pass

    return JsonResponse({'ok': True, 'updated': activities.count()})
