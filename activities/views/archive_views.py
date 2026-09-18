"""归档管理：查看已归档活动 + 归档/取消归档操作"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.shortcuts import render, get_object_or_404

from core.utils import visible_qs
from ..models import Activity
from ..utils import log_activity


@login_required
def archive_list(request):
    """已归档活动列表"""
    archived = visible_qs(Activity, request.user, include_archived=True).filter(
        archived_at__isnull=False
    ).order_by('-archived_at').prefetch_related('tags')

    return render(request, 'activities/archive_list.html', {
        'archived': archived,
    })


@login_required
def archive_activity(request, activity_id):
    """归档单个活动（JSON 端点）"""
    if request.method != 'POST':
        return JsonResponse({'error': '仅支持 POST'}, status=405)

    activity = get_object_or_404(Activity, id=activity_id, user=request.user)
    if activity.archived_at:
        return JsonResponse({'ok': False, 'error': '该活动已归档'})

    activity.archived_at = timezone.now()
    activity.save(update_fields=['archived_at'])
    log_activity(request.user, activity, 'edited', '归档活动')

    return JsonResponse({'ok': True, 'archived_count': 1})


@login_required
def unarchive_activity(request, activity_id):
    """取消归档（JSON 端点）"""
    if request.method != 'POST':
        return JsonResponse({'error': '仅支持 POST'}, status=405)

    activity = get_object_or_404(
        Activity.objects.filter(archived_at__isnull=False),
        id=activity_id, user=request.user
    )

    activity.archived_at = None
    activity.save(update_fields=['archived_at'])
    log_activity(request.user, activity, 'edited', '取消归档')

    return JsonResponse({'ok': True})
