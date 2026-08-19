from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.views.decorators.http import require_POST

from .forms import ActivityForm
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


@login_required
def activity_create(request):
    """新建活动"""
    if request.method == 'POST':
        form = ActivityForm(request.POST, user=request.user)
        if form.is_valid():
            activity = form.save(commit=False)
            activity.user = request.user
            activity.save()
            form.save_m2m()
            form.save_participants(activity)
            messages.success(request, f'活动「{activity.name}」已创建')
            return redirect('activities:activity_detail', activity.id)
    else:
        form = ActivityForm(user=request.user)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '新建活动',
    })


@login_required
def activity_edit(request, activity_id):
    """编辑活动"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)

    if request.method == 'POST':
        form = ActivityForm(request.POST, instance=activity, user=request.user)
        if form.is_valid():
            form.save()
            form.save_participants(activity)
            messages.success(request, f'活动「{activity.name}」已更新')
            return redirect('activities:activity_detail', activity.id)
    else:
        form = ActivityForm(instance=activity, user=request.user)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '编辑活动',
        'activity': activity,
    })


@login_required
@require_POST
def activity_delete(request, activity_id):
    """删除活动"""
    activity = get_object_or_404(Activity, id=activity_id, user=request.user)
    name = activity.name
    activity.delete()
    messages.success(request, f'活动「{name}」已删除')
    return redirect('activities:activity_list')
