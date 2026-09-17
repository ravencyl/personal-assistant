"""活动表单页：独立创建 / 编辑 / 删除"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.tags import apply_tags
from core.utils import get_visible, visible_qs

from ..forms import ActivityForm
from ..models import Activity, Participant
from ..utils import edit_summary, log_activity, snapshot_activity
from ._common import _user_tag_names


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
            # tags 已改为普通文本字段（core.Tag M2M），ModelForm 不再代管，视图落库
            apply_tags(activity, form.cleaned_data.get('tags'))
            form.save_participants(activity)
            children = form.save_children(activity)
            expense = form.save_cost(activity)
            log_activity(request.user, activity, 'created')
            for child in children:
                log_activity(request.user, child, 'created', f'随父活动「{activity.name}」一并创建')
            messages.success(request, f'活动「{activity.name}」已创建'
                           + (f'，已记入费用 ¥{expense.amount}' if expense else ''))
            # 列表页弹窗提交（fetch 带 HX-Request 头）：回 JSON 由前端跳转；独立页路径保持 302 不变
            if request.htmx:
                return JsonResponse({'ok': True,
                                     'redirect': reverse('activities:activity_detail', args=[activity.id])})
            return redirect('activities:activity_detail', activity.id)
        if request.htmx:
            # 弹窗提交校验失败：重渲染弹窗表单体（同一份 ActivityForm 校验，仅换呈现容器），
            # 以 422 让前端区分「校验不过」与「创建成功」
            return render(request, 'activities/_activity_form_modal_body.html', {
                'form': form,
                'all_participants': list(visible_qs(Participant, request.user).values_list('name', flat=True)),
                'all_tags': _user_tag_names(request.user),
            }, status=422)
    else:
        form = ActivityForm(user=request.user)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '新建活动',
        'all_participants': list(visible_qs(Participant, request.user).values_list('name', flat=True)),
        'all_tags': _user_tag_names(request.user),
    })


@login_required
def activity_edit(request, activity_id):
    """编辑活动（超级用户编辑他人活动时保持原属主）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    owner = activity.user

    if request.method == 'POST':
        old = snapshot_activity(activity)
        form = ActivityForm(request.POST, instance=activity, user=owner)
        if form.is_valid():
            form.save()
            apply_tags(activity, form.cleaned_data.get('tags'))
            form.save_participants(activity)
            log_activity(request.user, activity, 'edited', edit_summary(old, activity))
            messages.success(request, f'活动「{activity.name}」已更新')
            return redirect('activities:activity_detail', activity.id)
    else:
        form = ActivityForm(instance=activity, user=owner)

    return render(request, 'activities/activity_form.html', {
        'form': form,
        'title': '编辑活动',
        'activity': activity,
        'children': activity.children.all(),
        'all_participants': list(Participant.objects.filter(user=owner).values_list('name', flat=True)),
        'all_tags': _user_tag_names(owner),
    })


@login_required
@require_POST
def activity_delete(request, activity_id):
    """删除活动"""
    activity = get_visible(Activity, request.user, id=activity_id)
    name = activity.name
    log_activity(request.user, activity, 'deleted')
    activity.delete()
    messages.success(request, f'活动「{name}」已删除')
    return redirect('activities:activity_list')
