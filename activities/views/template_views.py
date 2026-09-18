"""活动模板端点：列出 / 创建 / 使用模板"""
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.utils import visible_qs

from ..models import Activity, ActivityTemplate
from ..utils import log_activity

logger = logging.getLogger(__name__)


@login_required
def template_list(request):
    """列出用户的活动模板（JSON 端点，供创建页下拉使用）"""
    templates = ActivityTemplate.objects.filter(user=request.user)
    return JsonResponse({
        'templates': [
            {
                'id': t.id,
                'name': t.name,
                'tags': t.default_tags,
                'participants': t.default_participants,
                'budget': float(t.default_budget) if t.default_budget else None,
                'description': t.default_description,
                'use_count': t.use_count,
            }
            for t in templates
        ],
    })


@login_required
@require_POST
def template_create(request):
    """从当前活动配置创建模板（JSON 端点）"""
    import json
    try:
        data = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        data = {}

    name = data.get('name', '').strip()
    if not name:
        return JsonResponse({'error': '请输入模板名称'}, status=400)

    # 可选：从已有活动复制配置
    source_id = data.get('source_activity_id')
    if source_id:
        try:
            source = visible_qs(Activity, request.user).get(pk=source_id)
            tags = ', '.join(source.tags.values_list('name', flat=True))
            participants = ', '.join(source.participants.values_list('name', flat=True))
            description = source.description
            budget = source.budget if hasattr(source, 'budget') else None
        except Activity.DoesNotExist:
            return JsonResponse({'error': '活动不存在'}, status=404)
    else:
        tags = data.get('tags', '')
        participants = data.get('participants', '')
        description = data.get('description', '')
        budget = data.get('budget')

    template = ActivityTemplate.objects.create(
        user=request.user,
        name=name,
        default_tags=tags,
        default_participants=participants,
        default_description=description,
        default_budget=budget,
    )

    return JsonResponse({
        'id': template.id,
        'name': template.name,
        'tags': template.default_tags,
        'participants': template.default_participants,
        'budget': float(template.default_budget) if template.default_budget else None,
    })


@login_required
@require_POST
def template_use(request, template_id):
    """从模板创建活动（JSON 端点）"""
    import json
    try:
        template = ActivityTemplate.objects.get(pk=template_id, user=request.user)
    except ActivityTemplate.DoesNotExist:
        return JsonResponse({'error': '模板不存在'}, status=404)

    try:
        data = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        data = {}

    # 创建新活动，预填模板字段
    activity = Activity.objects.create(
        user=request.user,
        name=template.name,
        description=template.default_description,
        start_date=data.get('start_date'),
    )

    # 设置标签
    if template.default_tags:
        from core.tags import apply_tags
        apply_tags(activity, template.default_tags)

    # 设置参与者
    if template.default_participants:
        from activities.utils import resolve_participants
        names = [n.strip() for n in template.default_participants.split(',') if n.strip()]
        participants, _skipped, _created = resolve_participants(request.user, names)
        activity.participants.set(participants)

    # 增加使用计数
    template.use_count += 1
    template.save(update_fields=['use_count'])

    log_activity(request.user, activity, 'created', f'从模板「{template.name}」创建')

    return JsonResponse({
        'id': activity.id,
        'name': activity.name,
        'url': f'/activities/{activity.id}/',
    })
