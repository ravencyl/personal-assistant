import hmac
import logging

import httpx

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.db import models
from django.utils import timezone

from .models import (Conversation, Message, TURN_TTL_SECONDS,
                     TURN_IDLE_GRACE_SECONDS)
from agents.models import AgentConfig, EnvironmentConfig
from agents.services import get_service
from core.agent_registry import (build_protocol_prompt, get_tool,
                                 make_action_token, orchestrator)
from core.utils import (visible_qs, get_visible, visible_child_qs, get_visible_child,
                        json_login_required)

logger = logging.getLogger(__name__)

# 等云端 AI 回一轮不再压在单个请求里（改走 turn 状态机 + 轮询）。
# 下面两个时长都住在 chat/models.py：TURN_TTL_SECONDS（本轮上限）、
# TURN_IDLE_GRACE_SECONDS（空回复宽限期），它们只约束浏览器轮询预算，与
# gunicorn --timeout 无关（单个请求里已没有 sleep）。

# 各类「本轮没回完」的文案。它们会**落库成 assistant 消息**（取消 / 空回复），
# 所以写成对用户可读、且重试时能被当成上下文看见的句子，不是日志串。
TURN_EMPTY_NOTE = '（AI 这轮没有返回内容，可能已超时。可以再问一次。）'
TURN_CANCELLED_NOTE = '（已停止这一轮的回答。）'
TURN_TIMEOUT_NOTE = f'这轮超过 {TURN_TTL_SECONDS} 秒还没回完，已停止等待。'
TURN_INTERRUPTED_NOTE = '上一轮没有完成，可以再问一次。'

# 新建对话默认用哪个 Agent（按 purpose 选，不再“取最近更新的那个”）。
# knowledge-agent 是用户在 Qoder 平台上手工配置过的那一个（version 6）：
# 工具集含 WebSearch/WebFetch/ImageSearch 且 permission_policy=always_allow（联网工具
# 不需人工批准，否则会话会卡在等授权）、instructions 里写了助手人设。
# AgentConfig.Meta.ordering = ['-updated_at']，靠 .first() 选会在每次 init_agents 后静默换人。
CHAT_AGENT_PURPOSE = 'knowledge'


@login_required
def conversation_list(request):
    """对话列表（支持搜索；超级用户可见全部对话）"""
    conversations = visible_qs(Conversation, request.user)
    agents = AgentConfig.objects.filter(is_active=True)

    # 搜索对话历史
    query = request.GET.get('q', '').strip()
    if query:
        # 搜消息内容：按会话可见范围过滤（超管能搜到他人会话里的消息），
        # 不再手写 is_superuser 分支判断
        message_qs = visible_child_qs(Message, request.user, 'conversation').filter(
            content__icontains=query)
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
    """对话详情

    上下文键必须用 chat_messages：`messages` 是 django.contrib.messages 注入的
    flash 变量，base.html 的提示条循环读它。视图用同名键塞 Message queryset 会把
    它顶掉，基模板就按 Message.__str__（「[user] 正文前 50 字」）把整段历史渲染到
    页面顶部，看起来像调试信息泄漏到线上（2026-08-31 用户截图反馈）。
    """
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    return render(request, 'chat/conversation_detail.html', {
        'conversation': conversation,
        'chat_messages': conversation.messages.all(),
        'turn_ttl': TURN_TTL_SECONDS,
    })


@login_required
def widget_messages(request, conversation_id):
    """浮窗加载历史消息（返回 HTML 片段）

    片段末尾带 turn_resume / turn_error 两个隐藏位：浮窗拿到就能判断“这一轮还在跑”
    并接上轮询，不需要另开一个“查状态”的口子。
    """
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    return render(request, 'chat/partials/widget_messages.html', {
        'widget_messages': conversation.messages.all(),
        'conversation': conversation,
        'turn_ttl': TURN_TTL_SECONDS,
    })


@json_login_required
@require_POST
def create_conversation(request):
    """创建新对话（HTMX/fetch/Accept JSON 请求返回 JSON，普通表单请求重定向到详情页）"""
    agent_id = request.POST.get('agent_id')
    if not agent_id:
        # 默认绑定固定用途的 Agent（见 CHAT_AGENT_PURPOSE 注释）
        agent_config = (AgentConfig.objects.filter(is_active=True, purpose=CHAT_AGENT_PURPOSE).first()
                        or AgentConfig.objects.filter(is_active=True).first())
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

        # 首帧下发意图协议指令（失败不阻断，降级为普通对话）
        try:
            service.send_message(session_data['id'], build_protocol_prompt())
        except Exception as e:
            logger.warning(f'首帧协议指令发送失败（对话 {conversation.id}）: {e}')

        # 首帧注入用户记忆（让 AI 天然「认识」用户）
        try:
            from memory.services import retrieve_memories, format_memory_for_injection
            top_memories = retrieve_memories(request.user, limit=10)
            if top_memories:
                memory_context = format_memory_for_injection(top_memories)
                service.send_message(session_data['id'], memory_context)
        except Exception as e:
            logger.warning(f'记忆注入失败（对话 {conversation.id}）: {e}')

        if request.htmx or request.headers.get('Accept') == 'application/json':
            return JsonResponse({
                'conversation_id': conversation.id,
                'title': conversation.title,
            })
        return redirect('chat:conversation_detail', conversation_id=conversation.id)
    except Exception as e:
        logger.error(f'Failed to create conversation: {e}')
        return JsonResponse({'error': str(e)}, status=500)


@json_login_required
@require_POST
def send_message(request, conversation_id):
    """发送消息：落库 + 发起，**立即返回**；等 AI 的循环交给 turn_poll

    旧实现在这个请求里 sleep 轮询到 AI 回完（最长 90s），代价是：一个提问占死
    一个 gunicorn worker（一共只 3 个）、用户不能取消、不能接着打字、刷新就丢这一轮。
    现在单个请求只做「存消息 + 一次 HTTP 发起」，毫秒级返回；状态全部落库，
    所以关掉页面再进来还能接着轮。详见 chat/models.Conversation 的状态机注释。
    """
    conversation = get_visible(Conversation, request.user, id=conversation_id)

    # 触发到期提醒（每次对话时检查）
    from core.models import check_due_reminders
    check_due_reminders(request.user)

    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '消息内容不能为空'}, status=400)

    if conversation.turn_active:
        if not conversation.turn_expired():
            # 一个 Qoder session 同时只能处理一轮（硬发会拿 409），
            # 所以这里给出可读提示而不是默默排队；前端保留草稿，不丢用户已打的字
            return _turn_response(request, conversation, {
                'error': '上一条还在处理中，等它回完或先点「停止」',
                'state': 'busy',
                'turn_state': conversation.turn_state,
            }, status=409)
        # TTL 残留（浏览器被关、worker 被杀、平台卡住）：开新轮前先把上一轮判为中断
        logger.info(f'对话 {conversation.id} 的上一轮没收尾（超时），开新一轮前强制中断')
        conversation.turn_state = Conversation.TURN_ERROR
        conversation.turn_idle_at = None

    # 保存用户消息（原始内容，不含上下文提示）
    user_msg = Message.objects.create(
        conversation=conversation,
        role='user',
        content=content,
        event_type='user.message'
    )

    # 规则兜底提取记忆（从用户消息中提取值得记住的信息）
    try:
        from memory.services import extract_memories_from_text
        extract_memories_from_text(request.user, content, source_message=user_msg)
    except Exception as e:
        logger.warning(f'规则记忆提取失败: {e}')

    ai_content = _build_ai_content(request, conversation, content)

    conversation.turn_state = Conversation.TURN_QUEUED
    conversation.turn_started_at = timezone.now()
    conversation.turn_idle_at = None
    conversation.turn_message = user_msg
    conversation.turn_prompt = ai_content
    conversation.status = 'processing'
    conversation.save(update_fields=['turn_state', 'turn_started_at', 'turn_idle_at',
                                     'turn_message', 'turn_prompt', 'status', 'updated_at'])

    # 先在本请求里试发一次（一次 HTTP 往返，通常百多毫秒）：
    #   sent → awaiting；busy（首帧协议还在跑）→ 保持 queued，由轮询重试发送
    outcome = _deliver(get_service(), conversation)
    if outcome == 'failed':
        conversation.turn_state = Conversation.TURN_ERROR
        conversation.status = 'idle'
        conversation.save(update_fields=['turn_state', 'status', 'updated_at'])
        return _turn_response(request, conversation, {
            'error': '发送失败，请重试', 'state': 'error',
            'retry_text': content, 'ttl': TURN_TTL_SECONDS,
        }, status=502)

    return _turn_response(request, conversation, {
        'state': 'processing',
        'turn_state': conversation.turn_state,
        'message_id': user_msg.id,
        'ttl': TURN_TTL_SECONDS,
        # 用户消息立即上屏：旧实现是等 AI 回完才跟回复一起出现，发一条得盯空白框几十秒
        'html': _message_fragment(request, user_msg),
    })


def _turn_response(request, conversation, payload, status=200):
    """fetch（Accept: application/json）要 JSON；无 JS 的普通表单提交带回对话页

    带回页面不是降级：本轮已经在后台发起并落库，详情页重渲染时进度条会自己接上去，
    比把一串 JSON 丢给用户看诚实得多。判据用 Accept 而不是 htmx：这个端点现在
    只由原生 fetch 消费（AGENTS.md 双协议约定：JSON 端点禁挂 hx-*，也不装 HX-Request）。

    status 必须透传：前端靠 `res.ok` 分流失败与成功，统一返 200 会把发送失败
    当成「已受理」并开始轮询一个已经不存在的轮次。
    """
    if 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse(payload, status=status)
    return redirect('chat:conversation_detail', conversation_id=conversation.id)


def _build_ai_content(request, conversation, content):
    """组装真正发给 Qoder 的文本：页面上下文 + 知识库注入 + 用户原文

    结果要落到 conversation.turn_prompt：重试发送时必须拿到同一份文本
    （把它塞进用户消息会污染历史，现有约定只存原文）。
    失败一律降级（铁律：任何环节失败都不得阻断对话）。
    """
    ai_content = content
    page_context = request.POST.get('page_context', '').strip()
    if page_context and page_context != 'home' and page_context != 'chat':
        ai_content = f'[页面提示: {page_context}]\n\n{content}'
    try:
        knowledge_context = _build_knowledge_context(conversation.user, content)
        if knowledge_context:
            ai_content = knowledge_context + ai_content
    except Exception as exc:
        logger.warning("knowledge injection failed: %s", exc)
    try:
        # 钉选放最后置前：它是用户显式点名的对象，优先级高于“按关键词猜”的知识库注入
        pinned = conversation.pinned_context()
        if pinned:
            ai_content = pinned + ai_content
    except Exception as exc:
        logger.warning("pin injection failed: %s", exc)
    return ai_content


def _deliver(service, conversation):
    """把本轮文本发到 Qoder。返回 'sent' | 'busy'（session 还在跑上一轮）| 'failed'

    409 不是错误：新建对话时会连发首帧协议指令 + 记忆注入两条且不等待，用户
    紧接着打字就必撞 409（旧实现因此在请求里同步等完首帧，这是「第一条消息特别慢」
    的真因）。现在保持 queued 交给轮询重试，发送请求本身立即返回。
    """
    try:
        service.send_message(conversation.session_id, conversation.turn_prompt)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            logger.info(f'会话 {conversation.id} 正忙（上一轮未结束），本轮稍后由轮询重发')
            return 'busy'
        logger.error(f'发送到 Qoder 失败（对话 {conversation.id}）: {e}')
        return 'failed'
    except Exception as e:
        logger.error(f'发送到 Qoder 失败（对话 {conversation.id}）: {e}')
        return 'failed'

    conversation.turn_state = Conversation.TURN_AWAITING
    conversation.turn_idle_at = None
    conversation.save(update_fields=['turn_state', 'turn_idle_at', 'updated_at'])
    return 'sent'


def _build_knowledge_context(user, text):
    """按用户消息检索相关知识库文章，构造注入上下文；无命中返回空串"""
    from knowledge.models import Article
    from knowledge.utils import search_articles

    articles = search_articles(visible_qs(Article, user), text, limit=3)
    if not articles:
        return ''
    context = '\n\n[相关知识库内容]\n'
    for a in articles:
        context += f'--- {a.title} ---\n{a.content[:800]}\n\n'
    return context


@json_login_required
@require_POST
def pin_conversation(request, conversation_id):
    """@ 钉选：把一个活动钉在本对话上（会话级状态，不是单条消息级）

    为什么做在会话级：用户讨论一个活动会连问好几轮（“预算改 3 万”“那还剩多少”
    “帮我记一笔费用”），每条消息单独钉一次等于没有钉。换对象就重新选一次。

    JSON 端点：由原生 fetch 消费，严禁挂 hx-*（AGENTS.md 双协议约定）。
    activity_id 为空 = 取消钉选。响应带 pin_bar 的 HTML 片段：钉选条的长相只有一
    份模板（服务端负责渲染），前端只换不拼字符串，跟消息气泡同一个道理。
    """
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    raw = (request.POST.get('activity_id') or '').strip()

    if not raw:
        conversation.pin_activity = None
        conversation.save(update_fields=['pin_activity', 'updated_at'])
        return _pin_response(request, conversation)
    if not raw.isdigit():
        return JsonResponse({'error': 'activity_id 必须是数字'}, status=400)

    from activities.models import Activity
    # 用 visible_qs 而不是 get_visible：后者报 Http404，fetch 拿回来的是 HTML 错误页
    activity = visible_qs(Activity, request.user).filter(id=int(raw)).first()
    if not activity:
        return JsonResponse({'error': '没找到这个活动（可能已删除或不属于你）'}, status=404)

    conversation.pin_activity = activity
    conversation.save(update_fields=['pin_activity', 'updated_at'])
    return _pin_response(request, conversation)


def _pin_response(request, conversation):
    """钉选结果统一出口（复用 _turn_response 的 Accept 分流，不写第二份判据）"""
    activity = conversation.pin_activity
    return _turn_response(request, conversation, {
        'pin': None if not activity else {'id': activity.id, 'name': activity.name},
        'html': render(request, 'chat/partials/pin_bar.html',
                       {'conversation': conversation}).content.decode(),
    })


@json_login_required
@require_GET
def pin_candidates(request):
    """@ 后的候选活动（JSON）。只搜可见范围；空 q 给「还在办的」，让 @ 一按就有东西可选"""
    from activities.models import Activity

    q = (request.GET.get('q') or '').strip()
    qs = visible_qs(Activity, request.user)
    if q:
        qs = qs.filter(models.Q(name__icontains=q) | models.Q(description__icontains=q)
                       | models.Q(tags__name__icontains=q)).distinct()
    else:
        qs = qs.filter(status__in=['planned', 'in_progress'])
    activity_ids = list(qs.order_by('start_date', '-id').values_list('id', flat=True)[:6])
    # 二次按 id 取对象会丢排序，所以排序只用在取 id 这一步；meta 里的日期/状态靠属性读
    rows = {a.id: a for a in Activity.objects.filter(id__in=activity_ids)}
    candidates = []
    for aid in activity_ids:
        a = rows.get(aid)
        if not a:
            continue
        meta = [a.get_status_display()]
        if a.date_range:
            meta.append(a.date_range)
        candidates.append({'id': a.id, 'name': a.name, 'meta': ' · '.join(meta)})
    return JsonResponse({'candidates': candidates})


@json_login_required
@require_GET
def turn_poll(request, conversation_id):
    """轮询本轮结果；AI 回完后在本请求内跑编排器 + 落库，返回消息 HTML 片段

    每个请求只做**一次**状态读取（几十毫秒，不 sleep），把过去压在单个请求里的
    循环摊成 N 个短请求。因此才能做到取消 / 刷新续上 / 边等边打字，也不再占 worker。

    返回：{'state': 'processing'|'done'|'error', 'html': 新增消息片段, 'changed': 活数据是否被写, 'ttl': …}

    多标签页并发轮询靠 claim_turn（条件 UPDATE）保证收尾只跑一遍：否则两个请求
    都拿到同一段 assistant 文本，会落库两条一模一样的回复，写操作工具还会被执行两次。
    """
    conversation = get_visible(Conversation, request.user, id=conversation_id)

    if conversation.turn_state in (Conversation.TURN_NONE, Conversation.TURN_DONE):
        return _poll_json({'state': 'done'})
    if conversation.turn_state == Conversation.TURN_ERROR:
        return _turn_error_json(request, conversation, TURN_INTERRUPTED_NOTE)
    if conversation.turn_expired():
        conversation.claim_turn(conversation.turn_state, Conversation.TURN_ERROR)
        conversation.status = 'idle'
        conversation.save(update_fields=['status', 'updated_at'])
        logger.info(f'对话 {conversation.id} 本轮超过 {TURN_TTL_SECONDS}s，定为中断')
        return _turn_error_json(request, conversation, TURN_TIMEOUT_NOTE)

    service = get_service()

    if conversation.turn_state == Conversation.TURN_QUEUED:
        # 需要（重）发：抢到发送权，避免两个标签页各发一遍（Qoder 会收到两条同样的消息）
        if not conversation.claim_turn(Conversation.TURN_QUEUED, Conversation.TURN_AWAITING):
            return _poll_json({'phase': 'queued'})
        outcome = _deliver(service, conversation)
        if outcome == 'busy':
            conversation.claim_turn(Conversation.TURN_AWAITING, Conversation.TURN_QUEUED)
            return _poll_json({'phase': 'session_busy'})
        if outcome == 'failed':
            conversation.claim_turn(Conversation.TURN_AWAITING, Conversation.TURN_ERROR)
            conversation.status = 'idle'
            conversation.save(update_fields=['status', 'updated_at'])
            return _turn_error_json(request, conversation, '发送失败，请重试')
        return _poll_json({'phase': 'sent'})

    # awaiting：读一次平台状态（与上面不同，这一步无副作用，不需要抢锁）
    try:
        result = service.poll_turn(conversation.session_id)
    except Exception as e:
        # 轮询本身失败不视为本轮失败（平台抽风、网络抖动），下一拍继续
        logger.warning(f'轮询本轮失败（对话 {conversation.id}）: {e}')
        return _poll_json({'phase': 'poll_error'})

    if result['state'] == 'processing':
        return _poll_json({})

    text = result['text']
    if result['state'] == 'empty':
        # 平台的 status 比事件写入快，得给宽限期才能区分「还没同步完」和「真的没回复」
        # （旧同步轮询里的 idle_hits >= 3 就是它，现在轮询跳到了请求之间，改用时间）
        if not conversation.turn_idle_at:
            conversation.turn_idle_at = timezone.now()
            conversation.save(update_fields=['turn_idle_at', 'updated_at'])
            return _poll_json({'phase': 'idle_grace'})
        if (timezone.now() - conversation.turn_idle_at).total_seconds() < TURN_IDLE_GRACE_SECONDS:
            return _poll_json({'phase': 'idle_grace'})
        text = ''

    if not conversation.claim_turn(Conversation.TURN_AWAITING, Conversation.TURN_FINALIZING):
        return _poll_json({'phase': 'finalizing'})

    msg, changed = _finalize_turn(conversation, text)
    return _poll_json({
        'state': 'done',
        'html': _message_fragment(request, msg),
        'changed': changed,
        'message_id': msg.id,
    })


def _poll_json(payload):
    """轮询响应统一出口：都带 ttl，前端据此定轮询预算（时长只在服务端一处定义）

    默认 state=processing，但 done 等终态必须显式传进来 —— 漏传会把「已完成」
    答成「还在跑」，前端就会一直轮下去。
    """
    payload.setdefault('state', 'processing')
    payload.setdefault('ttl', TURN_TTL_SECONDS)
    return JsonResponse(payload)


def _turn_error_json(request, conversation, note):
    """中断响应：除了状态字段，额外带上服务端渲染的气泡 HTML

    气泡只有一份模板（turn_error.html）：页面在轮询中碰到中断时由前端 append，
    刷新后则由模板自己渲染（见 conversation_detail / widget_messages 里的 turn_error include），
    两条路径共用模板、但不会同时出现（刷新后 resume 只对活跃状态生效）。
    """
    payload = {
        'state': 'error',
        'message': note,
        'retry_text': conversation.turn_message.content if conversation.turn_message else '',
        'html': render(request, 'chat/partials/turn_error.html', {
            'conversation': conversation, 'note': note,
        }).content.decode(),
    }
    payload['ttl'] = TURN_TTL_SECONDS
    return JsonResponse(payload)


@json_login_required
@require_POST
def turn_cancel(request, conversation_id):
    """停止本轮：请平台取消 + 落一条「已停止」的 assistant 消息

    cancel_session 是 best-effort：平台可能已自己结束（此时取消会报错），
    但本地状态无论如何都要收尾，否则这一轮会一直挂到 TTL。
    落一条 assistant 消息而不是只改状态：否则历史里留下一条永远没有回应的用户消息，
    刷新后看不出发生过什么。
    """
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    if not conversation.turn_active:
        return JsonResponse({'state': conversation.turn_state})

    try:
        get_service().cancel_session(conversation.session_id)
    except Exception as e:
        logger.info(f'取消本轮失败（多数是平台已自行结束）: {e}')

    msg, _ = _finalize_turn(conversation, '', note=TURN_CANCELLED_NOTE)
    return JsonResponse({'state': 'done', 'html': _message_fragment(request, msg)})


def _message_fragment(request, msg):
    """把一条消息渲染成 HTML 片段（与旧同步路径共用同一个模板，前端 append 逻辑不分叉）

    changed 不再靠往 HTML 尾部插隐藏 div 传递（那东西会永远留在消息流里）：
    轮询响应的 JSON 里有 changed 字段。【confirm_action】那条 HTMX 路径仍然用
    隐藏 div 传（片段里没有别的通道），base.html 的 htmx:afterSwap 监听在读它。
    """
    return render(request, 'chat/partials/message_pair.html', {'messages_pair': [msg]}).content.decode()


def _finalize_turn(conversation, assistant_text, note=None):
    """编排 + 落库本轮结果，并把 turn 收尾。返回 (Message, 活数据是否变更)

    assistant_text 为空串表示平台这轮确实没产出（超时 / 取消 / 空回复）：仍然落一条
    assistant 消息，否则历史里会留下一条永远没有回应的用户消息。
    容错铁律：编排器抛异常时降级为原始文本落库，绝不吞掉 AI 已回的正文。
    """
    text = assistant_text or (note or TURN_EMPTY_NOTE)
    try:
        content, payload, changed = orchestrator.process(conversation.user, text)
    except Exception as e:
        logger.error(f'编排本轮回复失败（对话 {conversation.id}），降级为纯文本: {e}')
        content, payload, changed = text, None, False

    assistant_msg = Message.objects.create(
        conversation=conversation,
        role='assistant',
        content=content,
        event_type='assistant.message',
        payload=payload,
    )
    # 待确认动作回填 HMAC 令牌（令牌含 message_id，只能在落库后生成）
    if payload and payload.get('action'):
        payload['action']['token'] = make_action_token(
            conversation.user, assistant_msg.id, 'confirm')
        assistant_msg.save(update_fields=['payload'])
    # 回填对话创建的活动来源消息（哪条消息创建了哪个活动）
    if payload and payload.get('created_activity_ids'):
        from activities.models import Activity
        Activity.objects.filter(
            id__in=payload['created_activity_ids'],
            user=conversation.user,
        ).update(source_message=assistant_msg)

    conversation.turn_state = Conversation.TURN_DONE
    conversation.turn_idle_at = None
    conversation.status = 'idle'
    conversation.save(update_fields=['turn_state', 'turn_idle_at', 'status', 'updated_at'])

    # 更新标题（如果是第一条消息）
    if conversation.title == '新对话':
        first_user_msg = conversation.messages.filter(role='user').first()
        if first_user_msg:
            conversation.title = first_user_msg.content[:50]
            conversation.save(update_fields=['title'])

    return assistant_msg, changed


@login_required
@require_POST
def confirm_action(request, message_id):
    """两步确认流：校验 HMAC 令牌后执行待确认动作（update / delete）"""
    message = get_visible_child(Message, request.user, 'conversation', id=message_id)
    payload = message.payload or {}
    action = payload.get('action') or {}

    def _render():
        response = render(request, 'chat/cards/_confirm_actions.html', {
            'msg': message,
            'action': action,
            'card': payload.get('card_data') or {},
        })
        # 确认后变更了活动数据：附标记，宿主页面据此提示/自动刷新
        if action.get('resolved') == 'confirmed' and action.get('changed'):
            response.content += b'<div data-activity-changed hidden></div>'
        return response

    if action.get('resolved'):
        return _render()

    if request.POST.get('decision') == 'cancel':
        action['resolved'] = 'cancelled'
        message.payload = payload
        message.save(update_fields=['payload'])
        return _render()

    token = request.POST.get('token', '')
    expected = make_action_token(request.user, message.id, 'confirm')
    tool = get_tool(action.get('tool') or '')
    if not hmac.compare_digest(token, expected) or not tool or not tool.get('apply'):
        action['resolved'] = 'failed'
        action['result'] = '确认无效或已过期，请重新发送消息再试'
        message.payload = payload
        message.save(update_fields=['payload'])
        return _render()

    try:
        result = tool['apply'](request.user, action.get('params') or {}) or {}
        action['resolved'] = 'confirmed'
        action['result'] = result.get('reply') or '操作完成'
        action['changed'] = bool(result.get('changed'))
    except Exception as e:
        logger.error(f'确认动作执行失败（消息 {message.id}）: {e}')
        action['resolved'] = 'failed'
        action['result'] = '执行失败，请稍后重试'
    message.payload = payload
    message.save(update_fields=['payload'])
    return _render()


@login_required
@require_POST
def archive_conversation(request, conversation_id):
    """归档对话"""
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    conversation.status = 'archived'
    conversation.save(update_fields=['status', 'updated_at'])

    # 归档即沉淀：留一条「讨论过什么 + 结论」的记忆，下次开新对话时 AI 能想起来。
    # 它只读库里的消息（不碰平台），所以放在 cancel 之前；失败不得影响归档本身。
    try:
        from memory.services import summarize_conversation_for_memory
        summarize_conversation_for_memory(conversation)
    except Exception as e:
        logger.warning(f'归档摘要写入记忆失败（对话 {conversation.id}）: {e}')

    # 归档时还有进行中的一轮：一并清掉，否则 turn 状态会永远挂着（下次进来还在转圈）
    if conversation.turn_active:
        conversation.reset_turn()

    # 尝试在 Qoder 平台取消
    try:
        service = get_service()
        service.cancel_session(conversation.session_id)
    except Exception:
        pass

    return redirect('chat:conversation_list')


@login_required
@require_POST
def conversation_rename(request, conversation_id):
    """重命名对话（POST title 字段，空串视为取消不改）"""
    conversation = get_visible(Conversation, request.user, id=conversation_id)
    title = (request.POST.get('title') or '').strip()
    if title:
        conversation.title = title[:255]
        conversation.save(update_fields=['title', 'updated_at'])
    # 无论改没改都回到详情页（列表页的 inline 表单也 redirect 回 detail，避免刷新重复提交）
    return redirect('chat:conversation_detail', conversation_id=conversation.id)


@login_required
@require_POST
def conversation_delete(request, conversation_id):
    """硬删除对话：先清掉平台侧活跃 session 与本地 turn 状态，再删 Conversation + Messages"""
    conversation = get_visible(Conversation, request.user, id=conversation_id)

    # 与 archive 同样的清理顺序：turn → 平台 cancel → 删库。任何一步失败都不阻断删除本身
    # （用户明确点了删除，不能因为平台超时把对话卡在「想删删不掉」的状态）。
    if conversation.turn_active:
        try:
            conversation.reset_turn()
        except Exception as e:
            logger.warning(f'删除对话 {conversation.id} 时 reset_turn 失败: {e}')

    try:
        service = get_service()
        service.cancel_session(conversation.session_id)
    except Exception:
        pass

    conversation.delete()
    return redirect('chat:conversation_list')
