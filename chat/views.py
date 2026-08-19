import logging

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.db import models

from .models import Conversation, Message
from agents.models import AgentConfig, EnvironmentConfig
from agents.services import get_service
from core.utils import visible_qs, get_visible

logger = logging.getLogger(__name__)


@login_required
def conversation_list(request):
    """对话列表（支持搜索；超级用户可见全部对话）"""
    conversations = visible_qs(Conversation, request.user)
    agents = AgentConfig.objects.filter(is_active=True)

    # 搜索对话历史
    query = request.GET.get('q', '').strip()
    if query:
        # 搜索消息内容，找到匹配的对话
        message_qs = Message.objects.filter(content__icontains=query)
        if not request.user.is_superuser:
            message_qs = message_qs.filter(conversation__user=request.user)
        matching_conv_ids = message_qs.values_list('conversation_id', flat=True).distinct()
        # 同时搜索对话标题
        conversations = conversations.filter(
            models.Q(id__in=matching_conv_ids) | models.Q(title__icontains=query)
        )

    # 预取每个对话的最后一条消息，避免模板中 N+1 查询
    conversations = conversations.prefetch_related(
        models.Prefetch(
            'messages',
            queryset=Message.objects.order_by('-created_at')[:1],
            to_attr='last_message_list',
        )
    )

    return render(request, 'chat/conversation_list.html', {
        'conversations': conversations,
        'agents': agents,
        'query': query,
    })


@login_required
def conversation_detail(request, conversation_id):
    """对话详情"""
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    messages = conversation.messages.all()
    return render(request, 'chat/conversation_detail.html', {
        'conversation': conversation,
        'messages': messages,
    })


@login_required
def widget_messages(request, conversation_id):
    """浮窗加载历史消息（返回 HTML 片段）"""
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    return render(request, 'chat/partials/widget_messages.html', {
        'widget_messages': conversation.messages.all(),
    })


@login_required
@require_POST
def create_conversation(request):
    """创建新对话（HTMX/fetch 请求返回 JSON，普通表单请求重定向到详情页）"""
    agent_id = request.POST.get('agent_id')
    if not agent_id:
        # 使用第一个可用的 agent
        agent_config = AgentConfig.objects.filter(is_active=True).first()
        if not agent_config:
            return JsonResponse({'error': '没有可用的 Agent 配置'}, status=400)
        agent_id = agent_config.agent_id

    # 获取 environment
    env_config = EnvironmentConfig.objects.filter(is_default=True).first()
    if not env_config:
        env_config = EnvironmentConfig.objects.first()
    if not env_config:
        return JsonResponse({'error': '没有可用的 Environment 配置'}, status=400)

    service = get_service()
    try:
        # 在 Qoder 平台创建 Session
        session_data = service.create_session(
            agent_id=agent_id,
            environment_id=env_config.env_id
        )

        # 本地创建对话记录
        conversation = Conversation.objects.create(
            user=request.user,
            session_id=session_data['id'],
            agent_id=agent_id,
            title=f'新对话',
            status='idle',
        )

        if request.htmx:
            return JsonResponse({
                'conversation_id': conversation.id,
                'title': conversation.title,
            })
        return redirect('chat:conversation_detail', conversation_id=conversation.id)
    except Exception as e:
        logger.error(f'Failed to create conversation: {e}')
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def send_message(request, conversation_id):
    """发送消息"""
    conversation = get_visible(Conversation, request.user, id=conversation_id)

    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '消息内容不能为空'}, status=400)

    # 保存用户消息
    user_msg = Message.objects.create(
        conversation=conversation,
        role='user',
        content=content,
        event_type='user.message'
    )

    # 发送到 Qoder
    service = get_service()
    try:
        service.send_message(conversation.session_id, content)
        conversation.status = 'processing'
        conversation.save(update_fields=['status', 'updated_at'])

        # 收集 assistant 响应（返回本轮创建的 Message，超时/无响应为 None）
        assistant_msg = _collect_response(service, conversation)

        # HTMX 请求返回 HTML 片段（含用户消息 + AI 回复），普通请求返回 JSON
        if request.htmx:
            pair = [m for m in [user_msg, assistant_msg] if m]
            return render(request, 'chat/partials/message_pair.html', {
                'messages_pair': pair,
            })

        return JsonResponse({
            'success': True,
            'response': assistant_msg.content if assistant_msg else '(无响应)',
        })
    except Exception as e:
        logger.error(f'Failed to send message: {e}')
        conversation.status = 'idle'
        conversation.save(update_fields=['status', 'updated_at'])
        return JsonResponse({'error': str(e)}, status=500)


def _collect_response(service, conversation):
    """等待并落库 AI 响应，返回本轮创建的 Message（超时/无响应返回 None）"""
    assistant_text = service.wait_for_response(conversation.session_id, timeout=120)

    assistant_msg = None
    if assistant_text:
        assistant_msg = Message.objects.create(
            conversation=conversation,
            role='assistant',
            content=assistant_text,
            event_type='assistant.message'
        )

    conversation.status = 'idle'
    conversation.save(update_fields=['status', 'updated_at'])

    # 更新标题（如果是第一条消息）
    if conversation.title == '新对话':
        first_user_msg = conversation.messages.filter(role='user').first()
        if first_user_msg:
            conversation.title = first_user_msg.content[:50]
            conversation.save(update_fields=['title'])

    return assistant_msg


@login_required
@require_POST
def archive_conversation(request, conversation_id):
    """归档对话"""
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    conversation.status = 'archived'
    conversation.save(update_fields=['status', 'updated_at'])

    # 尝试在 Qoder 平台取消
    try:
        service = get_service()
        service.cancel_session(conversation.session_id)
    except Exception:
        pass

    return redirect('chat:conversation_list')
