import json
import logging

from django.shortcuts import render, get_object_or_404, redirect
from django.http import StreamingHttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.conf import settings
from django.db import models

from .models import Conversation, Message
from agents.models import AgentConfig, EnvironmentConfig
from agents.services import get_service

logger = logging.getLogger(__name__)


@login_required
def conversation_list(request):
    """对话列表（支持搜索）"""
    conversations = Conversation.objects.filter(user=request.user)
    agents = AgentConfig.objects.filter(is_active=True)

    # 搜索对话历史
    query = request.GET.get('q', '').strip()
    if query:
        # 搜索消息内容，找到匹配的对话
        matching_conv_ids = Message.objects.filter(
            conversation__user=request.user,
            content__icontains=query,
        ).values_list('conversation_id', flat=True).distinct()
        # 同时搜索对话标题
        conversations = conversations.filter(
            models.Q(id__in=matching_conv_ids) | models.Q(title__icontains=query)
        )

    return render(request, 'chat/conversation_list.html', {
        'conversations': conversations,
        'agents': agents,
        'query': query,
    })


@login_required
def conversation_detail(request, conversation_id):
    """对话详情"""
    conversation = get_object_or_404(
        Conversation,
        id=conversation_id,
        user=request.user
    )
    messages = conversation.messages.all()
    return render(request, 'chat/conversation_detail.html', {
        'conversation': conversation,
        'messages': messages,
    })


@login_required
@require_POST
def create_conversation(request):
    """创建新对话"""
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

        return redirect('chat:conversation_detail', conversation_id=conversation.id)
    except Exception as e:
        logger.error(f'Failed to create conversation: {e}')
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def send_message(request, conversation_id):
    """发送消息"""
    conversation = get_object_or_404(
        Conversation,
        id=conversation_id,
        user=request.user
    )

    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '消息内容不能为空'}, status=400)

    # 保存用户消息
    Message.objects.create(
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

        # 收集 assistant 响应
        assistant_content = _collect_response(service, conversation)

        # HTMX 请求返回 HTML 片段（含用户消息 + AI 回复），普通请求返回 JSON
        if request.htmx:
            pair = [m for m in [
                conversation.messages.filter(role='user').last(),
                conversation.messages.filter(role='assistant').last(),
            ] if m]
            return render(request, 'chat/partials/message_pair.html', {
                'messages_pair': pair,
            })

        return JsonResponse({
            'success': True,
            'response': assistant_content,
        })
    except Exception as e:
        logger.error(f'Failed to send message: {e}')
        conversation.status = 'idle'
        conversation.save(update_fields=['status', 'updated_at'])
        return JsonResponse({'error': str(e)}, status=500)


def _collect_response(service, conversation):
    """轮询收集 AI 响应"""
    import time
    max_wait = 120  # 最多等待 120 秒
    start_time = time.time()

    while time.time() - start_time < max_wait:
        session_data = service.get_session(conversation.session_id)
        status = session_data.get('status', '')

        if status == 'idle':
            # 处理完成，获取最新消息
            events = service.get_session_events(conversation.session_id, limit=50)
            assistant_text = _extract_assistant_message(events)

            if assistant_text:
                Message.objects.create(
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

            return assistant_text or '(无响应)'

        time.sleep(1)

    # 超时
    conversation.status = 'idle'
    conversation.save(update_fields=['status', 'updated_at'])
    return '(响应超时，请稍后重试)'


def _extract_assistant_message(events):
    """从事件列表中提取 assistant 消息"""
    messages = []
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                event_type = event.get('type', '')
                if 'assistant' in event_type or event_type == 'agent.message':
                    content_list = event.get('content', [])
                    for c in content_list:
                        if isinstance(c, dict) and c.get('type') == 'text':
                            messages.append(c.get('text', ''))
    return '\n'.join(messages) if messages else ''


@login_required
@require_POST
def archive_conversation(request, conversation_id):
    """归档对话"""
    conversation = get_object_or_404(
        Conversation,
        id=conversation_id,
        user=request.user
    )
    conversation.status = 'archived'
    conversation.save(update_fields=['status', 'updated_at'])

    # 尝试在 Qoder 平台取消
    try:
        service = get_service()
        service.cancel_session(conversation.session_id)
    except Exception:
        pass

    return redirect('chat:conversation_list')


@login_required
def message_stream(request, conversation_id):
    """SSE 消息流（用于实时推送）"""
    conversation = get_object_or_404(
        Conversation,
        id=conversation_id,
        user=request.user
    )

    def event_stream():
        service = get_service()
        try:
            for event in service.stream_events(conversation.session_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingHttpResponse(
        event_stream(),
        content_type='text/event-stream'
    )
