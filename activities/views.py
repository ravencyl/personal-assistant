from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum

from .models import Activity


@login_required
def activity_list(request):
    """活动列表"""
    activities = Activity.objects.filter(user=request.user)

    status_filter = request.GET.get('status', '')
    if status_filter:
        activities = activities.filter(status=status_filter)

    # 一次性聚合子活动数量、费用合计、关联任务数，避免 N+1
    activities = activities.annotate(
        sub_count=Count('children', distinct=True),
        task_count=Count('tasks', distinct=True),
        children_cost_sum=Sum('children__cost'),
    )

    return render(request, 'activities/activity_list.html', {
        'activities': activities,
        'status_filter': status_filter,
        'status_choices': Activity.STATUS_CHOICES,
    })


@login_required
def activity_detail(request, activity_id):
    """活动详情（含子活动和关联任务）"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)

    return render(request, 'activities/activity_detail.html', {
        'activity': activity,
        'children': activity.children.all(),
        'tasks': activity.tasks.all(),
        'participants': activity.participants.all(),
    })
