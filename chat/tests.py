"""对话协议 / 编排器的「通用问答逃生舱」测试

背景（线上真实故障）：首帧协议把 AI 钉成「只输出意图 JSON 的活动管理助手」，
于是「去美国出差要准备什么」这类问题被吸进 knowledge_search，本地没命中就直接回
「没有找到与…相关的知识库文章」，而云端 Agent 其实早就挂着 WebSearch/WebFetch 却从不被调用。

这里锁住五件事，防止再次退化：
1. 协议里必须有「通用问题→联网直答」的出口，且不再自称「活动管理助手」
2. 未注册工具的意图（ask）与纯自然语言回复都要能原样透传给用户
3. 含 `{}` 但无 intent 的自然语言不得被误当成协议 JSON
4. 等 AI 的超时必须显著小于 gunicorn --timeout（否则 worker 在响应途中被杀）
5. 对话输入框的 Enter 只能换行、不得提交（误发一条要等几十秒，代价高），
   且多行框的高度自适应不能在未渲染状态下把高度算成 0
"""
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.models import QuerySet
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from chat import views as chat_views
from core.layout_asserts import assert_desktop_two_columns
from chat.models import (Conversation, Message, TURN_IDLE_GRACE_SECONDS,
                         TURN_TTL_SECONDS)
from core.agent_registry import (INTENT_TOOL_MAP, build_protocol_prompt,
                                 extract_intent, orchestrator)

User = get_user_model()

# 测试里引用时长一律走这两个别名：把秒数写进断言文案就等于没锁
models_TTL = TURN_TTL_SECONDS
models_GRACE = TURN_IDLE_GRACE_SECONDS



class ProtocolEscapeHatchTest(SimpleTestCase):
    """首帧协议：两类消息分流必须写清楚"""

    def setUp(self):
        self.prompt = build_protocol_prompt()

    def test_persona_is_not_limited_to_activities(self):
        self.assertNotIn('活动管理助手', self.prompt)
        self.assertIn('智能助手', self.prompt)

    def test_prompt_opens_web_search_escape_hatch(self):
        for kw in ('WebSearch', 'WebFetch', '联网', 'ask'):
            self.assertIn(kw, self.prompt, f'协议缺少联网问答出口：{kw}')

    def test_prompt_forbids_knowledge_search_as_generic_fallback(self):
        # 通用问题不得用 knowledge_search 交差，也不得只回「没有找到」
        self.assertIn('不要用 knowledge_search', self.prompt)

    def test_ask_intent_is_not_bound_to_a_tool(self):
        """ask 故意不注册工具：走编排器透传，系统不再做任何动作"""
        self.assertNotIn('ask', INTENT_TOOL_MAP)

    def test_prompt_guides_ai_to_use_memory_search(self):
        """规则 9：协议必须显式引导 AI 主动调 memory_search 回忆用户信息

        工具已注册、首帧已注入 Top 10，但模型不知道自己能查——补一行规则就能让
        现有能力活起来。这条断言防止规则被误删或改写后失去引导作用。
        """
        self.assertIn('memory_search', self.prompt)
        self.assertIn('回忆', self.prompt)
        self.assertIn('不要凭印象编造', self.prompt)


class OrchestratorPassthroughTest(SimpleTestCase):
    """编排器：逃生舱的两条透传路径"""

    def setUp(self):
        self.user = User(username='u1')

    def test_ask_intent_reply_passes_through(self):
        text = '{"intent": "ask", "reply": "美国出差主要三件事：EVUS、签证有效期、行程单。参考：a.gov"}'
        content, payload, changed = orchestrator.process(self.user, text)
        self.assertIn('EVUS', content)
        self.assertIsNone(payload)
        self.assertFalse(changed)

    def test_plain_text_answer_passes_through(self):
        text = '根据美国海关官网，随身携带超 $10,000 现金需申报。'
        content, payload, changed = orchestrator.process(self.user, text)
        self.assertEqual(content, text)
        self.assertIsNone(payload)

    def test_text_with_braces_but_no_intent_is_not_misparsed(self):
        """自然语言里夹着 JSON 片段（如配置示例）时不得被当成协议回复"""
        text = '示例配置：{"timeout": 30} 这样写就可以了。'
        self.assertIsNone(extract_intent(text))
        content, _, _ = orchestrator.process(self.user, text)
        self.assertEqual(content, text)


class KnowledgeSearchFallbackTextTest(TestCase):
    """knowledge.search 无命中时的文案：面向用户，不泄露模型指令

    用 TestCase（非 Simple）：visible_qs 会把 user 作为外键条件落到查询里，需已保存的实例。
    """

    def test_no_hit_text_points_to_web_and_leaks_no_prompt(self):
        from knowledge.agent_tools import tool_knowledge_search

        user = User.objects.create_user(username='u2')
        with patch('knowledge.agent_tools.search_articles', return_value=[]):
            result = tool_knowledge_search(user, {'keyword': '美国出差准备'})
        reply = result['reply']
        self.assertIn('知识库里没有', reply)
        self.assertIn('上网', reply)          # 告诉用户可以直接联网问
        self.assertNotIn('请改用', reply)      # 不能写成给模型看的指令
        self.assertNotIn('intent', reply)


class AiTimeoutChainTest(SimpleTestCase):
    """超时链：请求内的 AI 等待必须短于 gunicorn 看门狗 —— 而聊天路径必须根本不等

    聊天原本是这条链上最紧的一环（`AI_WAIT_TIMEOUT=90` 顶在 gunicorn 180 下面，
    改任何一端都要同步改另一端）。改成异步 turn 之后它**退出**这条链：/send/ 与
    /turn/ 里不允许出现任何长等待，本轮上限退化为业务预算（TURN_TTL_SECONDS），
    不再受看门狗约束。报告/快速输入那些仍在请求内同步调 AI 的路径继续受约束。
    """

    # 只扫可能在请求里被调用的 app 目录：直接 rglob 整个项目会把 venv（上万个文件）走一遍
    APP_DIRS = ('activities', 'core', 'knowledge', 'notes', 'memory', 'agents', 'chat')

    def _gunicorn_timeout(self):
        deploy_md = (Path(__file__).resolve().parent.parent / 'DEPLOY.md').read_text(encoding='utf-8')
        m = re.search(r'--timeout (\d+)', deploy_md)
        self.assertIsNotNone(m, 'DEPLOY.md 必须写明 gunicorn --timeout')
        return int(m.group(1))

    def test_chat_request_path_contains_no_blocking_wait(self):
        """聊天视图里再出现 wait_for_response / sleep 就是回退成同步阻塞占 worker"""
        src = (Path(__file__).resolve().parent / 'views.py').read_text(encoding='utf-8')
        self.assertNotIn('wait_for_response', src, '聊天视图又在请求里等 AI 回完')
        self.assertNotIn('time.sleep', src, '聊天视图里出现了同步等待')

    def test_in_request_ai_round_trips_stay_under_the_watchdog(self):
        """仍在请求内同步调 AI 的地方（周报、AI 快速输入解析…）超时上限必须留在看门狗之内

        扫调用点取最大值而不是钉一个常量：新增一处拿大数的 `ai_round_trip` 就该被抓住，
        否则又会回到「改一个值要记得改三个」的口头约定。

        必须排除测试文件本身：上一版没排除，它把自己 docstring 里的举例当成真调用点报错了
        （静态扫描器兼不排注释/文档，就会靠改文案才能跑绿 —— 那种锁不可信）。
        """
        root = Path(__file__).resolve().parent.parent
        worst, worst_at = 0, None
        for app in self.APP_DIRS:
            for py in (root / app).rglob('*.py'):
                if 'management' in py.parts or 'migrations' in py.parts:
                    continue          # 管理命令由 cron 跑，没有 gunicorn 看门狗
                if py.name.startswith('test'):
                    continue          # 测试里的举例不是调用点
                for m in re.finditer(r'ai_round_trip\([^)]*?timeout=(\d+)',
                                     py.read_text(encoding='utf-8')):
                    if int(m.group(1)) > worst:
                        worst, worst_at = int(m.group(1)), py.relative_to(root)
        self.assertGreater(worst, 0, '扫不到任何带 timeout 的 ai_round_trip，说明调用方式或正则变了')
        self.assertLess(worst, self._gunicorn_timeout(),
                        f'{worst_at} 在请求内等 {worst}s，顶到了看门狗 {self._gunicorn_timeout()}s')

    def test_turn_budget_is_bounded(self):
        """本轮上限要有界：太短会掐断正常的联网问答，太长会让用户对着进度条空转"""
        self.assertLessEqual(60, TURN_TTL_SECONDS)
        self.assertLessEqual(TURN_TTL_SECONDS, 300)
        self.assertLess(TURN_IDLE_GRACE_SECONDS, TURN_TTL_SECONDS)


class ChatInputEnterBehaviorTest(SimpleTestCase):
    """对话输入框：Enter 只做换行，提交只允许点「发送」按钮

    起因：输入框原本是单行 input，对话页还显式绑了 Enter → requestSubmit()，
    打字打字就误发一条（AI 一轮要等几十秒，误发代价高）。改成 textarea 后
    Enter 天然换行、也不会隐式提交表单。这里锁住结构，防止以后又改回 input
    或重新加回 Enter 监听。
    """

    ENTER_SUBMIT = re.compile(r"(?:key|keyCode|which)\s*(?:===|==)\s*['\"]?(?:Enter|13)")

    def _tpl(self, *parts):
        return (Path(__file__).resolve().parent.parent / 'templates' / Path(*parts)).read_text(
            encoding='utf-8')

    def test_both_chat_inputs_are_multiline_textareas(self):
        """两个输入框都必须是 textarea：单行 input 物理上换不了行"""
        page = self._tpl('chat', 'conversation_detail.html')
        base = self._tpl('base.html')
        self.assertIn('id="message-input"', page)
        self.assertIn('id="chat-input"', base)
        self.assertIn('<textarea name="content" id="message-input"', page)
        self.assertIn('<textarea name="content" id="chat-input"', base)
        for html in (page, base):
            self.assertNotIn('<input type="text" name="content"', html)

    def test_no_enter_key_submit_handler_anywhere_in_chat_inputs(self):
        for parts in (('chat', 'conversation_detail.html'), ('base.html',)):
            html = self._tpl(*parts)
            self.assertEqual(self.ENTER_SUBMIT.findall(html), [],
                             f'{parts} 里又出现了 Enter 提交逻辑')

    def test_placeholder_tells_user_enter_is_newline(self):
        """行为变了要写进提示，否则用户会以为发送键坏了"""
        page = self._tpl('chat', 'conversation_detail.html')
        self.assertIn('Enter 换行', page)

    def test_multiline_input_css_caps_height_and_disables_drag_resize(self):
        """高度由 paFitTextarea 接管：必须关掉手动拖拽并限高，否则会撑坏浮窗布局"""
        css = (Path(__file__).resolve().parent.parent / 'static' / 'css' / 'custom.css') \
            .read_text(encoding='utf-8')
        self.assertIn('.chat-input', css)
        block = css.split('.chat-input', 1)[1].split('}', 1)[0]
        self.assertIn('resize: none', block)
        self.assertIn('max-height', block)

    def test_single_auto_grow_implementation_shared_by_both_inputs(self):
        """对话页与浮窗共用同一个自适应实现，不允许各写一份"""
        base = self._tpl('base.html')
        self.assertIn('window.paFitTextarea', base)
        self.assertEqual(base.count('function paFitTextarea'), 0)
        self.assertEqual(base.count('ta.style.height = \'auto\';'), 1, '自适应逻辑只能有一份')
        self.assertIn('data-auto-grow', base)

    def test_auto_grow_never_runs_on_unrendered_textarea(self):
        """未渲染的 textarea 不能参与高度计算（实测踩过：浮窗输入框被压成 0 高）

        浮窗面板初始是 display:none，此时 scrollHeight 恒为 0，页面加载时跑那一遍
        初始化会把 #chat-input 的 style.height 写成 0px，placeholder 直接裁掉半截。
        """
        base = self._tpl('base.html')
        fit = base.split('window.paFitTextarea = function', 1)[1].split('};', 1)[0]
        self.assertIn('getClientRects', fit, '缺少「元素是否已渲染」的守卫')
        self.assertLess(fit.find('getClientRects'), fit.find('ta.style.height = Math.min'),
                        '守卫必须在写高度之前，否则 0 已经写回去了')
        # 面板展开时要补一次拟合，否则被守卫跳过后就再没人算高度
        show_view = base.split('function showChatView', 1)[1].split('}', 1)[0]
        self.assertIn('paFitTextarea', show_view, '面板展开后没补高度拟合')

    def test_quick_fab_yields_while_chat_panel_is_open(self):
        """快记 FAB 会盖住浮窗的「发送」按钮，聊天面板展开期间必须让开

        两个浮窗都锚定 right-4，实测面板底边只比快记 FAB 顶边高 12px，重叠 23px；
        既然只能点按钮发送，就不能让按钮被另一个 FAB 盖住大半。
        """
        base = self._tpl('base.html')
        self.assertIn('function setQuickFabHidden', base)
        self.assertEqual(base.count('setQuickFabHidden('), 3, '1 定义 + 2 展开入口（FAB 点击 / paChatAsk）')


class ConversationDetailContextTest(TestCase):
    """对话详情不得占用模板上下文里的 messages

    线上真实反馈（2026-08-31 用户截图）：/chat/<id>/ 顶部出现一整排「[user] …」
    「[assistant] …」卡片，看着像调试信息泄漏。根因不是有人写了调试模板，而是视图
    把 Message queryset 塞进了 `messages` 这个键 —— 它是 django.contrib.messages
    注入的 flash 变量，base.html 的提示条 {% for message in messages %}{{ message }}
    于是按 Message.__str__（「[role] 正文前 50 字」）把整段历史渲染了出来；
    代价还包括这个页面的 flash 提示全部失效。
    """

    def setUp(self):
        self.user = User.objects.create_user('chatuser', password='pw')
        self.conv = Conversation.objects.create(user=self.user, session_id='sess_ctx',
                                               agent_id='agent_ctx', title='测试对话')
        Message.objects.create(conversation=self.conv, role='user',
                               content='我有一个疑问，情侣之间对话应该是怎么样子的'
                                       '有时候会被对方投诉说我不尊重他的对话')
        Message.objects.create(conversation=self.conv, role='assistant',
                               content='这个问题的核心其实不是话术')
        self.client.login(username='chatuser', password='pw')
        self.url = f'/chat/{self.conv.id}/'

    def test_history_is_not_dumped_with_role_brackets(self):
        """页面不得出现 Message.__str__ 形式的「[user] / [assistant]」文本"""
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('[user]', html)
        self.assertNotIn('[assistant]', html)

    def test_history_still_renders_normally(self):
        """改名不能把正常渲染一起改掉：正文、中文角色标签都要在"""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '这个问题的核心其实不是话术')
        self.assertContains(resp, 'AI 助手')

    def test_messages_key_is_left_to_the_flash_framework(self):
        """上下文里的 messages 必须是 flash 存储而不是 Message queryset"""
        resp = self.client.get(self.url)
        self.assertNotIsInstance(resp.context['messages'], QuerySet,
                                 'messages 被视图占用了，base.html 的 flash 块会渲染历史消息')
        self.assertIsInstance(resp.context['chat_messages'], QuerySet)

    def test_no_chat_template_iterates_bare_messages(self):
        """模板层兜底：只允许 base.html 的 flash 块读裸 messages"""
        templates = (Path(__file__).resolve().parent.parent / 'templates').rglob('*.html')
        offenders = [str(t) for t in templates
                     if re.search(r'{%\s*for\s+\w+\s+in\s+messages\s*%}', t.read_text(encoding='utf-8'))
                     and t.name != 'base.html']
        self.assertEqual(offenders, [], f'这些模板又占了裸 messages：{offenders}')


class ConversationListDesktopLayoutTest(TestCase):
    """对话列表页桌面两列布局回归锁（右列 = 搜索与检索状态）

    本页是 rail-first：右列整块在 DOM 里排在主内容流之前，移动端顺序
    （未配置提示 → 搜索 → 会话列表）与改造前逐块一致。
    必须带真实会话渲染：空列表时列归属与顺序锁会空跑。
    """
    TEMPLATE = Path(__file__).resolve().parent.parent / 'templates' / 'chat' / 'conversation_list.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client.login(username='raven', password='test')
        Conversation.objects.create(user=self.user, session_id='sess_lay',
                                    title='新西兰之旅怎么安排')
        self.html = self.client.get('/chat/').content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('新西兰之旅怎么安排', '会话卡')],
            right=[('搜索对话历史...', '搜索框')],
            mobile_order=['搜索对话历史...', '新西兰之旅怎么安排'],
            rail_first=True)

    def test_ai_not_configured_banner_stays_above_columns(self):
        """阻断性提示不进右列：右列 sticky，滚到列表底部时提示会被滚走"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        self.assertLess(src.index('AI 服务未配置'), src.index('class="page-cols'),
                        '未配置提示应留在两列区之上')


# ==================== 异步 turn 收发（Phase A）====================

def http_error(status):
    """造一个带 status_code 的 httpx.HTTPStatusError（视图按 409 分流，别的状态算失败）"""
    import httpx
    req = httpx.Request('POST', 'https://example.test/')
    return httpx.HTTPStatusError('boom', request=req, response=httpx.Response(status, request=req))


class FakeQoderService:
    """替掉真实云端调用：记录调用、按脚本返回轮询状态

    **故意不提供 wait_for_response**：只要视图回退成「在请求里等 AI 回完」，
    调用就会 AttributeError 让测试当场炸掉 —— 这比断言「没被调用」更硬。
    """

    def __init__(self, send_raises=None, poll_script=None):
        self.sent = []
        self.cancelled = 0
        self.poll_calls = 0
        self._send_raises = send_raises
        self.poll_script = list(poll_script or [])

    def send_message(self, session_id, text):
        if self._send_raises:
            raise self._send_raises
        self.sent.append(text)
        return {}

    def poll_turn(self, session_id):
        self.poll_calls += 1
        if self.poll_script:
            return self.poll_script.pop(0)
        return {'state': 'processing', 'text': ''}

    def cancel_session(self, session_id):
        self.cancelled += 1
        return {}


class ChatTurnModelTest(TestCase):
    """turn 状态机本身的三条基础件：抢占、活跃判定、超时判定"""

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_m', agent_id='ag_m', title='新对话')

    def test_only_one_claimer_wins(self):
        self.conv.turn_state = Conversation.TURN_AWAITING
        self.conv.save(update_fields=['turn_state'])
        self.assertTrue(self.conv.claim_turn(Conversation.TURN_AWAITING, Conversation.TURN_FINALIZING))
        # 第二个轮询者必须抢不到，否则会各跑一遍编排器 → 双份回复 + 写工具执行两次
        other = Conversation.objects.get(id=self.conv.id)
        self.assertFalse(other.claim_turn(Conversation.TURN_AWAITING, Conversation.TURN_FINALIZING))

    def test_active_states_cover_exactly_the_inflight_window(self):
        for state, expected in [
            (Conversation.TURN_NONE, False), (Conversation.TURN_QUEUED, True),
            (Conversation.TURN_AWAITING, True), (Conversation.TURN_FINALIZING, True),
            (Conversation.TURN_DONE, False), (Conversation.TURN_ERROR, False),
        ]:
            with self.subTest(state=state):
                self.conv.turn_state = state
                self.assertEqual(self.conv.turn_active, expected)

    def test_expired_only_for_active_turns_past_the_budget(self):
        self.assertFalse(self.conv.turn_expired())          # 无进行中轮次
        self.conv.turn_state = Conversation.TURN_AWAITING
        self.assertFalse(self.conv.turn_expired())          # 活跃但没有开始时间
        self.conv.turn_started_at = timezone.now()
        self.assertFalse(self.conv.turn_expired())          # 刚起
        self.conv.turn_started_at = timezone.now() - timedelta(seconds=models_TTL + 1)
        self.assertTrue(self.conv.turn_expired())           # 超预算

    def test_reset_turn_clears_every_turn_field(self):
        msg = Message.objects.create(conversation=self.conv, role='user', content='x')
        self.conv.turn_state = Conversation.TURN_QUEUED
        self.conv.turn_message = msg
        self.conv.turn_prompt = 'ctx'
        self.conv.turn_started_at = timezone.now()
        self.conv.turn_idle_at = timezone.now()
        self.conv.save()
        self.conv.reset_turn()
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_NONE)
        self.assertIsNone(self.conv.turn_message)
        self.assertEqual(self.conv.turn_prompt, '')
        self.assertIsNone(self.conv.turn_started_at)
        self.assertIsNone(self.conv.turn_idle_at)


class ChatSendAsyncTest(TestCase):
    """发送路径必须立即返回，不在请求里等 AI"""

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_s', agent_id='ag_s', title='新对话')
        self.url = f'/chat/{self.conv.id}/send/'

    def _post(self, service, content='明天去跑步'):
        with patch('chat.views.get_service', return_value=service):
            return self.client.post(self.url, {'content': content, 'page_context': 'home'},
                                    HTTP_ACCEPT='application/json')

    def test_send_returns_before_the_reply_exists(self):
        service = FakeQoderService()
        resp = self._post(service)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['state'], 'processing')
        self.assertEqual(resp.json()['turn_state'], Conversation.TURN_AWAITING)
        self.assertEqual(self.conv.messages.filter(role='user').count(), 1)
        # 核心：这轮还没有任何 assistant 消息（旧实现在这里会等 90s 直到拿到回复）
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 0)
        self.assertEqual(len(service.sent), 1)

    def test_send_response_carries_the_user_message_fragment(self):
        html = self._post(FakeQoderService()).json()['html']
        self.assertIn('明天去跑步', html)
        self.assertIn('class="chat-message user', html)

    def test_send_keeps_the_composed_prompt_for_retries(self):
        """turn_prompt 存的是组装后的文本（页面上下文 + 用户原文），用户消息只存原文"""
        service = FakeQoderService()
        with patch('chat.views.get_service', return_value=service):
            self.client.post(self.url, {'content': '帮我改个时间',
                                        'page_context': '用户正在查看活动「沟通」'},
                             HTTP_ACCEPT='application/json')
        self.conv.refresh_from_db()
        self.assertIn('[页面提示:', self.conv.turn_prompt)
        self.assertIn('帮我改个时间', self.conv.turn_prompt)
        self.assertEqual(self.conv.turn_message.content, '帮我改个时间')
        self.assertNotIn('页面提示', self.conv.messages.filter(role='user').first().content)

    def test_send_is_refused_while_a_turn_is_inflight(self):
        self.conv.turn_state = Conversation.TURN_AWAITING
        self.conv.turn_started_at = timezone.now()
        self.conv.save()
        before = self.conv.messages.count()
        resp = self._post(FakeQoderService())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['state'], 'busy')
        # 拒发要发生在落库之前：否则历史里留下一条永远发不出去的孤儿消息
        self.assertEqual(self.conv.messages.count(), before)

    def test_platform_busy_keeps_the_turn_queued_instead_of_failing(self):
        """撞 409 不是失败：保持 queued 交给轮询重发（旧实现在这里同步等完首帧）"""
        service = FakeQoderService(send_raises=http_error(409))
        resp = self._post(service)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['turn_state'], Conversation.TURN_QUEUED)
        self.assertEqual(self.conv.messages.filter(role='user').count(), 1)

    def test_hard_send_failure_marks_the_turn_errored(self):
        service = FakeQoderService(send_raises=http_error(500))
        resp = self._post(service)
        self.assertEqual(resp.status_code, 502)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_ERROR)
        self.assertEqual(self.conv.status, 'idle')

    def test_plain_form_post_redirects_back_instead_of_dumping_json(self):
        """无 JS 的表单提交（不带 Accept: application/json）要带回对话页，不能把 JSON 甩给用户"""
        with patch('chat.views.get_service', return_value=FakeQoderService()):
            resp = self.client.post(self.url, {'content': '你好'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], f'/chat/{self.conv.id}/')


class ChatTurnPollTest(TestCase):
    """轮询端点：什么时候继续等、什么时候收尾、怎么保证只收尾一次"""

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_p', agent_id='ag_p', title='新对话',
            status='processing', turn_state=Conversation.TURN_AWAITING,
            turn_started_at=timezone.now())
        self.msg = Message.objects.create(conversation=self.conv, role='user', content='查一下活动')
        self.conv.turn_message = self.msg
        self.conv.save()
        self.url = f'/chat/{self.conv.id}/turn/'

    def _poll(self, service):
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.get(self.url, HTTP_ACCEPT='application/json')
        return resp.status_code, (resp.json() if resp.status_code == 200 else {})

    def test_idle_conversation_is_reported_done(self):
        self.conv.turn_state = Conversation.TURN_NONE
        self.conv.save()
        _, data = self._poll(FakeQoderService())
        self.assertEqual(data['state'], 'done')

    def test_still_running_session_keeps_polling_without_writing(self):
        _, data = self._poll(FakeQoderService(poll_script=[{'state': 'processing', 'text': ''}]))
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 0)

    def test_ready_text_is_finalized_into_one_message(self):
        service = FakeQoderService(poll_script=[
            {'state': 'ready', 'text': '{"intent":"chitchat","reply":"在的"}'}])
        _, data = self._poll(service)
        self.assertEqual(data['state'], 'done')
        self.assertIn('在的', data['html'])
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 1)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_DONE)
        self.assertEqual(self.conv.status, 'idle')

    def test_second_poll_after_done_does_not_duplicate_the_reply(self):
        """两个标签页同时轮询时只能落一条：重复落库会把写操作工具执行两遍

        注意这条**不能单独**当抢锁的锁：第一轮跑完后状态已是 done，第二轮在入口
        就短路返回，即使把 claim_turn 完全删掉它也会绿（实测如此）。真正抢锁靠下面
        那条「已被别人收尾中」的用例，两条合起来才封住双落库。
        """
        service = FakeQoderService(poll_script=[
            {'state': 'ready', 'text': '第一遍'},
            {'state': 'ready', 'text': '第一遍'}])
        _, first = self._poll(service)
        _, second = self._poll(service)
        self.assertEqual(first['state'], 'done')
        self.assertEqual(second['state'], 'done')
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 1)

    def test_turn_being_finalized_elsewhere_is_not_executed_again(self):
        """另一个请求已在收尾（状态已是 finalizing）：本请求必须空手而回

        这是 claim_turn 唯一真正的锁：两个轮询者都从 awaiting 看到 ready 文本时，
        没抢锁的那个如果照样跑编排器，就会落两条一样的回复、并把写工具执行两次。
        """
        self.conv.turn_state = Conversation.TURN_FINALIZING
        self.conv.save(update_fields=['turn_state'])
        _, data = self._poll(FakeQoderService(
            poll_script=[{'state': 'ready', 'text': '不能重复执行'}]))
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 0,
                         '抢不到锁却继续收尾 → 双份回复 + 写操作重放')

    def test_empty_reply_waits_the_grace_period_before_concluding(self):
        service = FakeQoderService(poll_script=[
            {'state': 'empty', 'text': ''}, {'state': 'empty', 'text': ''}])
        _, d1 = self._poll(service)
        self.assertEqual(d1['state'], 'processing')
        self.assertEqual(d1['phase'], 'idle_grace')
        self.conv.refresh_from_db()
        self.assertIsNotNone(self.conv.turn_idle_at)
        # 宽限期内仍不算失败（平台 status 比事件写入快，早判会把正常回复丢掉）
        _, d2 = self._poll(service)
        self.assertEqual(d2['state'], 'processing')
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 0)

    def test_empty_reply_after_grace_lands_a_readable_note(self):
        self.conv.turn_idle_at = timezone.now() - timedelta(seconds=models_GRACE + 1)
        self.conv.save()
        _, data = self._poll(FakeQoderService(poll_script=[{'state': 'empty', 'text': ''}]))
        self.assertEqual(data['state'], 'done')
        note = self.conv.messages.filter(role='assistant').first()
        self.assertIsNotNone(note, '空回复也要落一条消息，否则历史里留着一条没人回应的问题')
        self.assertIn('没有返回内容', note.content)

    def test_expired_turn_becomes_error_with_a_retry_bubble(self):
        self.conv.turn_started_at = timezone.now() - timedelta(seconds=models_TTL + 5)
        self.conv.save()
        _, data = self._poll(FakeQoderService())
        self.assertEqual(data['state'], 'error')
        self.assertIn('data-retry-text', data['html'])
        self.assertIn('查一下活动', data['html'])       # 原文带在按钮上，点了就重发
        self.assertEqual(data['retry_text'], '查一下活动')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_ERROR)

    def test_queued_turn_is_delivered_by_the_poll(self):
        """409 留在队列里的轮次由轮询补发，且只补发一次"""
        self.conv.turn_state = Conversation.TURN_QUEUED
        self.conv.turn_prompt = '完整组装文本'
        self.conv.save()
        service = FakeQoderService()
        _, data = self._poll(service)
        self.assertEqual(data['phase'], 'sent')
        self.assertEqual(service.sent, ['完整组装文本'])
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_AWAITING)

    def test_poll_send_that_hits_busy_goes_back_to_queued(self):
        service = FakeQoderService(send_raises=http_error(409))
        self.conv.turn_state = Conversation.TURN_QUEUED
        self.conv.turn_prompt = 'x'
        self.conv.save()
        _, data = self._poll(service)
        self.assertEqual(data['phase'], 'session_busy')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_QUEUED)

    def test_transient_poll_error_does_not_fail_the_turn(self):
        class Boom(FakeQoderService):
            def poll_turn(self, session_id):
                raise RuntimeError('平台抽风')

        _, data = self._poll(Boom())
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(data['phase'], 'poll_error')
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.turn_active)

    def test_poll_never_leaks_another_users_conversation(self):
        other = User.objects.create_user('o', password='p')
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url, HTTP_ACCEPT='application/json').status_code, 404)


class ChatTurnCancelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_c', agent_id='ag_c', title='新对话',
            status='processing', turn_state=Conversation.TURN_AWAITING,
            turn_started_at=timezone.now())
        self.url = f'/chat/{self.conv.id}/turn/cancel/'

    def _cancel(self, service):
        with patch('chat.views.get_service', return_value=service):
            return self.client.post(self.url, HTTP_ACCEPT='application/json')

    def test_cancel_finalizes_locally_even_if_the_platform_call_fails(self):
        """cancel_session 是 best-effort：平台可能已经自己结束了（这时取消会报错）"""
        class NoCancel(FakeQoderService):
            def cancel_session(self, session_id):
                raise RuntimeError('already finished')

        resp = self._cancel(NoCancel())
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_DONE)
        self.assertEqual(self.conv.status, 'idle')
        note = self.conv.messages.filter(role='assistant').first()
        self.assertIsNotNone(note)
        self.assertIn('已停止', note.content)
        self.assertIn('已停止', resp.json()['html'], '气泡由服务端渲染，前端不再拼一份')

    def test_cancel_on_a_finished_turn_writes_nothing(self):
        self.conv.turn_state = Conversation.TURN_DONE
        self.conv.save()
        service = FakeQoderService()
        resp = self._cancel(service)
        self.assertEqual(resp.json()['state'], Conversation.TURN_DONE)
        self.assertEqual(service.cancelled, 0)
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 0)


class ChatTurnTemplateWiringTest(SimpleTestCase):
    """异步收发的模板/前端接线锁

    这类锁看着像字符串比对，守的都是「静默失效」的坑：漏 include 一个 partial、
    进度条用了 hidden+flex、或者表单还挂着 hx-* 把 JSON 当文本插进 DOM，
    都不会有报错，只有用户看得到坏掉。
    """

    def _tpl(self, *parts):
        return (Path(__file__).resolve().parent.parent / 'templates' / Path(*parts)).read_text(
            encoding='utf-8')

    def test_detail_form_has_no_htmx_attributes(self):
        """JSON 端点必须原生 fetch 消费（AGENTS.md 双协议约定），hx-* 会把 JSON 当文本插入

        切片必须切到「这个标签自己的 >」：上一版切到第一个 {% csrf_token %}，而它属于顶部
        「归档对话」表单（位置在发送表单之前）→ 切片为空 → assertNotIn 恒真，
        一条看起来严谨、实际空跑的锁（拿 hx-post 变异反证才发现）。
        """
        detail = self._tpl('chat', 'conversation_detail.html')
        start = detail.index('<form id="message-form"')
        form = detail[start:detail.index('>', start)]
        self.assertIn('action=', form, '表单要保留 action 供 JS 取 URL（也兼容无 JS 提交）')
        self.assertNotIn('hx-', form, '对话表单还挂着 hx-*')
        self.assertNotIn('hx-', detail[detail.index('<textarea'):detail.index('</form>', start)],
                         '输入框侧也不能再带 hx-*')
        self.assertIn('chat-turn.js', detail)
        self.assertIn('PaChatTurn', detail)

    def test_one_message_renderer_for_page_and_panel(self):
        """详情页、浮窗历史、轮询新片段共用 _message.html：两份实现必然漂移成两种长相"""
        for rel in (('chat', 'partials', 'message_pair.html'),
                   ('chat', 'partials', 'widget_messages.html')):
            self.assertIn('chat/partials/_message.html', self._tpl(*rel), str(rel))
        detail = self._tpl('chat', 'conversation_detail.html')
        self.assertIn('{% include "chat/partials/_message.html" %}', detail)

    def test_status_bar_avoids_hidden_flex_display_conflict(self):
        """进度条不能同时用 hidden 与 flex/items-center 这类 display 工具类：
        去掉 hidden 后拿到哪个 display 取决于样式表顺序，会让进度条显示成不确定状态"""
        bar = self._tpl('chat', 'partials', 'turn_status.html')
        tag = bar[bar.index('<div data-turn-status'):bar.index('>', bar.index('<div data-turn-status'))]
        self.assertIn('hidden', tag)
        for clash in ('flex', 'grid', 'items-center', 'inline'):
            self.assertNotIn(clash, tag, f'进度条容器不该带 display 类 {clash}')
        self.assertIn('data-turn-cancel', bar)
        self.assertIn('data-turn-text', bar)

    def test_error_bubble_is_rendered_once_per_view(self):
        """error 时服务端渲染气泡，active 时只渲染隐藏的 resume 数据 —— 两个都渲染会出现双气泡"""
        detail = self._tpl('chat', 'conversation_detail.html')
        self.assertIn("{% if conversation.turn_state == 'error' %}", detail)
        self.assertIn('{% include "chat/partials/turn_resume.html" %}', detail)
        resume = self._tpl('chat', 'partials', 'turn_resume.html')
        self.assertIn('{% if conversation.turn_active %}', resume)
        self.assertIn('data-retry-text', self._tpl('chat', 'partials', 'turn_error.html'))

    def test_both_hosts_load_the_shared_flow_once(self):
        """chat-turn.js 由 base.html 统一引入（详情页与浮窗同一个实例来源）"""
        base = self._tpl('base.html')
        self.assertIn("{% staticv 'js/chat-turn.js' %}", base)
        self.assertEqual(base.count('PaChatTurn('), 1, '浮窗侧只应有一个 PaChatTurn 实例')
        self.assertIn('paPageContext', base, '详情页要复用同一份页面上下文判定')


class ChatTurnFlowJsTest(SimpleTestCase):
    """chat-turn.js 的源码锁

    这些不是风格检查：浏览器实测时就是「漏一个 Accept 头」让发送看着失败（消息其实
    已落库、轮次已发起，用户再点一次就是重复提问）。JS 里的这种错没有异常抛出，
    只有用户能看到坏掉，所以拿源码断言钉住。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        cls.flow = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.flow_code = cls._strip_js_comments(cls.flow)
        cls.base = (root / 'templates' / 'base.html').read_text(encoding='utf-8')
        cls.detail = (root / 'templates' / 'chat' / 'conversation_detail.html').read_text(encoding='utf-8')

    @staticmethod
    def _strip_js_comments(src):
        """剔掉 JS 注释后再做禁用词扫描

        不剔就是一句谎话：「禁止调 htmx.process」的说明注释自己包含这个词，锁会报假失败
        （本项目第三次坑到这个形状：前两次是模板注释里的 sm:/max-w-4xl）。
        改文案避开关键词是假修法：下一个人重新写句注释又会碎。
        本文件没有 `//` 形态的字符串（无带 scheme 的 URL），所以单行注释剔除不会误伤。
        """
        no_block = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        return re.sub(r'^\s*//.*$', '', no_block, flags=re.M)

    def _send_fetch_block(self):
        start = self.flow.index("apiFetch(u.send")
        return self.flow[start:self.flow.index('})', start)]

    def test_send_goes_through_the_shared_json_outlet(self):
        """发送必须走 apiFetch：Accept 与 401 处理都在那个出口里

        以前这里逐调用点断言 'Accept': 'application/json'，现在收口到一个出口，
        断言改成「有没有绕开出口」（绕开就同时丢了 Accept 和登录过期处理）。
        """
        self.assertIn('apiFetch(u.send', self._send_fetch_block())

    def test_flow_does_not_call_htmx_process(self):
        """AGENTS.md：手动 htmx.process 会造成双重绑定与旧节点引用残留

        只扫剔了注释的代码：本文件的注释里就写了“绝不能再调 htmx.process()”，
        拿全文做禁用词比对会把说明当成违规。
        """
        self.assertNotIn('htmx.process', self.flow_code)
        self.assertIn('htmx.process', self.flow, '注释里的禁令说明也在 —— 证明上面剔注释真的在起作用')

    def test_flow_keeps_the_input_editable_while_waiting(self):
        """等回复期间只锁发送按钮，输入框必须能继续打字 —— 这是异步化的目的之一"""
        self.assertNotIn('input.disabled', self.flow_code, '又把输入框禁掉了')
        self.assertIn('sendBtn.disabled', self.flow_code, '发送按钮的锁定是保留项，别一起删了')

    def test_no_dangling_reference_to_the_old_loading_nodes(self):
        """旧的 #loading / #chat-loading 元素已删除：残留引用不会报错，只会在点击时抛 undefined

        同样只扫剔了注释的代码（JS 注释、模板注释里的说明文字不算引用）。
        """
        from core.layout_asserts import code_only
        for src, name in ((self.flow_code, 'chat-turn.js'),
                          (code_only(self.base), 'base.html'),
                          (code_only(self.detail), '详情页')):
            self.assertNotIn("getElementById('loading')", src, name)
            self.assertNotIn('chat-loading', src, name)

    def test_each_host_has_exactly_one_status_bar(self):
        """两处各一个进度条（include 同一模板）；多一个就会有两个「停止」按钮互相抢事件"""
        self.assertEqual(self.base.count('chat/partials/turn_status.html'), 1)
        self.assertEqual(self.detail.count('chat/partials/turn_status.html'), 1)

    def test_panel_halts_flow_before_loading_another_conversation(self):
        """切对话必须先 halt()：否则上一个对话的轮询会往新对话的消息流里 append 回复"""
        base = self.base
        opener = base[base.index('function openConversation'):base.index('// 快记 FAB 跟聊天面板几何重叠')]
        self.assertIn('.halt()', opener)
        self.assertIn('data-turn-resume', opener, '历史加载完要接上进行中的轮次')
        self.assertIn('return fetch(', opener, 'openConversation 必须返回 Promise，paChatAsk 要等历史渲染完再发')

    def test_cancel_does_not_broadcast_a_data_change(self):
        """取消没写过任何数据：无条件广播会让点「停止」后弹出「活动数据已更新」（实测踩到）"""
        start = self.flow.index('function stop()')
        body = self.flow[start:self.flow.index('\n        }', start)]
        self.assertIn('if (d.changed && opts.onActivityChanged)', body,
                      'stop() 里的广播必须看 d.changed')
        self.assertNotIn('if (opts.onActivityChanged)', body, '不允许无条件广播数据已变更')
        self.assertIn('apiFetch(u.cancel', body, 'cancel 也必须走共用出口')


class ChatMarkdownRenderTest(TestCase):
    """AI 回复在消息片段里真的走 Markdown，用户消息保持字面纯文本"""

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_md', agent_id='ag_md', title='新对话')

    def _fragment(self, role, content):
        from django.template.loader import render_to_string
        msg = Message.objects.create(conversation=self.conv, role=role, content=content)
        return render_to_string('chat/partials/_message.html', {'msg': msg})

    def test_assistant_reply_is_rendered(self):
        html = self._fragment('assistant', '## 小结\n- **甲**：值得去\n参考 https://example.com/a')
        self.assertIn('<h2 class="md-h">小结</h2>', html)
        self.assertIn('<li><strong>甲</strong>：值得去</li>', html)
        self.assertIn('<a class="md-link', html)
        self.assertNotIn('- **甲**', html, '原始标记不该出现在页面上')

    def test_user_message_stays_literal(self):
        """用户打的就是字面量：`**粗**` 不该被渲染成粗体，也不该被切开成块级元素"""
        html = self._fragment('user', '**粗** 和 | 表格 | 都按原文显示')
        self.assertIn('**粗** 和 | 表格 | 都按原文显示', html)
        self.assertIn('whitespace-pre-wrap', html)
        self.assertNotIn('<strong>', html)

    def test_rendered_container_does_not_keep_pre_wrap(self):
        """渲染出的块级结构若还带 whitespace-pre-wrap，会跟 <br> 叠成双倍行距"""
        html = self._fragment('assistant', '一段\n两段')
        body = html[html.index('markdown-content'):html.index('</div>')]
        self.assertNotIn('whitespace-pre-wrap', body)

    def test_raw_html_in_reply_cannot_reach_the_dom(self):
        """模板只允许通过 ai_markdown 一个出口输出 HTML：模型被诱导吐出的 <script>
        只能以文字形态出现在气泡里。

        做成渲染断言而不是静态扫「模板里不得出现 |safe」：扫类名/管道词的锁必须
        先剔注释才不会假失败（本项目已踩三次），而这条直接看最终 HTML，注释怎么写都不影响。
        """
        html = self._fragment('assistant',
                              '你好 <script>alert(1)</script>\n<img src=x onerror=alert(2)>')
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img ', html)

    def test_copy_button_only_on_assistant_bubbles(self):
        """「复制」只在 AI 气泡上（自己的话不需要复制），且必须是 type=button

        做成渲染断言而不是扫模板：要讲清「只在 assistant 分支」就得在源码里切范围，
        而切空的锁会静默空跑（本项目踩过）；渲染结果两个分支直接就能比对。"""
        assistant = self._fragment('assistant', '**结论**：值得去')
        user = self._fragment('user', '那个活动怎么去')
        self.assertIn('data-copy-msg', assistant)
        self.assertNotIn('data-copy-msg', user)
        self.assertIn('<button type="button" data-copy-msg', assistant)


class ChatQuickActionsTest(SimpleTestCase):
    """常驻快捷 chips 与复制按钮的接线锁

    chips 只在两个宿主（对话详情页、右下角浮窗）各挂一份，处理逻辑只有一份（在
    chat-turn.js 里）。这类“两处入口 + 一处实现”的东西漂起来的方式就是：某一处
    忘 include、或者有人直接在模板里写 onclick 叉出第二份实现。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        js = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.js_code = ChatTurnFlowJsTest._strip_js_comments(js)
        cls.chips = (root / 'templates' / 'chat' / 'partials' / 'quick_chips.html')\
            .read_text(encoding='utf-8')
        cls.base = (root / 'templates' / 'base.html').read_text(encoding='utf-8')
        cls.detail = (root / 'templates' / 'chat' / 'conversation_detail.html')\
            .read_text(encoding='utf-8')

    # ── chips ──

    def test_chips_render_with_tappable_buttons(self):
        from django.template.loader import render_to_string
        html = render_to_string('chat/partials/quick_chips.html', {'chips_id': 'x-chips'})
        prompts = re.findall(r'data-chip="([^"]+)"', html)
        self.assertGreaterEqual(len(prompts), 3, 'chips 太少了就不叫常驻入口')
        self.assertTrue(all(p.strip() for p in prompts), '空 prompt 的 chip 点下去什么也不会发生')
        # 漏了 type 的 button 在表单里会被当成提交按钮（这里是防御性钳制）
        self.assertEqual(html.count('<button'), len(prompts))
        self.assertNotIn('<button class', html)
        # chips 在移动端是主要入口：触控区走全站统一的 .tap-target（≤44px 规则），
        # 而不是在 .chat-chip 里另外写死一个高度（那样桌面与移动只能顾一头）
        self.assertEqual(html.count('class="chat-chip tap-target"'), len(prompts))

    def test_chip_height_defers_to_tap_target_on_mobile(self):
        """移动端不得在 .chat-chip 里自己写死高度

        真机实测踩到：基础规则里写 min-height:2rem 与 .tap-target 同特异度、靠顺序
        取胜，把 44px 触控区压回 32px（CSS 里没有任何报错）。桌面要定高只能进
        min-width 媒体查询（全站唯一断点 768px）。
        """
        from core.layout_asserts import CSS_PATH, css_rules
        rules = css_rules(CSS_PATH.read_text(encoding='utf-8'), '.chat-chip')
        self.assertTrue(rules, '.chat-chip 样式不见了')
        tall = [r for r in rules if 'min-height' in r['body']]
        for r in tall:
            self.assertIn('min-width', r['media'],
                          '基础规则里定高会压掉 .tap-target：%s' % r['selectors'])

    def test_chips_mounted_on_both_surfaces_with_distinct_ids(self):
        self.assertIn('quick_chips.html', self.detail)
        self.assertIn('quick_chips.html', self.base)
        ids = re.findall(r'chips_id="([^"]+)"', self.detail + self.base)
        self.assertEqual(len(ids), 2, '两个宿主各挂一份，多了就是写了第三处')
        self.assertEqual(len(set(ids)), 2, 'id 撞了 getElementById 只会拿到第一个')
        for src, name in ((self.detail, '详情页'), (self.base, '浮窗')):
            self.assertIn('chipsEl: document.getElementById', src,
                          '%s 没有把 chips 容器交给 PaChatTurn' % name)

    def test_chip_click_fills_draft_instead_of_sending(self):
        """点 chip 只负责填入输入框：Enter 不提交是全站约定，直接发出去就剥夺了改两字的机会"""
        # 锚点全部取自己代码（剔注释后的 js_code）：拿注释文本当锚点必碎
        block = self.js_code[self.js_code.index('if (chipsEl) {'):self.js_code.index('function halt()')]
        self.assertGreater(len(block.strip()), 200, '切片切空了，这条锁就是假的')
        self.assertIn('input.value', block)
        self.assertIn('input.focus()', block)
        self.assertNotIn('send(', block, 'chip 不得直接发送')

    # ── 复制 ──

    def test_copy_handler_uses_rendered_text_and_has_fallback(self):
        self.assertIn('data-copy-msg', self.js_code)
        self.assertIn("querySelector('.md-body')", self.js_code,
                      '必须复制渲染后的可读正文，不是 Markdown 源文')
        self.assertIn('navigator.clipboard', self.js_code)
        self.assertIn('document.execCommand', self.js_code, '无剪贴板 API 的环境要有降级')
        self.assertIn("btn.textContent = '复制'", self.js_code, '反馈文案必须回弹，不能停在「已复制」')

    def test_no_blocking_dialogs_in_chat_flow(self):
        """聊天里的错误一律进进度条/气泡，不用原生 alert（浮窗里弹阻塞框特别累）"""
        self.assertNotIn('alert(', self.js_code)
        self.assertNotIn('confirm(', self.js_code)


class ChatPinTest(TestCase):
    """@ 钉选：会话级上下文（模型注入 + 两个 JSON 端点）

    钉选的价值全在「注入的是现状」：模型拿到预算/已花/参与者才能直接算「还剩多少」，
    只给一个 ID 它得先调一次查询工具，多一轮往返就多一次出错机会。
    """

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.other = User.objects.create_user('o', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_p', agent_id='ag_p', title='钉选')
        from activities.models import Activity, Participant
        today = timezone.localdate()
        self.activity = Activity.objects.create(
            user=self.user, name='新西兰旅游', status='planned',
            start_date=today + timedelta(days=30),
            budget=20000, description='住皇后镇，含跳伞')
        self.activity.participants.add(Participant.objects.create(user=self.user, name='YYX'))
        self.activity.tags.add('新西兰')
        self.foreign = Activity.objects.create(user=self.other, name='别人的活动')

    @property
    def pin_url(self):
        return f'/chat/{self.conv.id}/pin/'

    # ── 注入文本 ──

    def test_pinned_context_carries_the_facts_the_model_needs(self):
        self.conv.pin_activity = self.activity
        text = self.conv.pinned_context()
        self.assertIn('[钉选对象]', text)
        for needle in ('新西兰旅游', f'ID={self.activity.id}', '状态：计划',
                       '预算：¥20000.00', '已花费：¥0.00', 'YYX', '住皇后镇'):
            self.assertIn(needle, text)
        # 不点名对象时默认就是它：这句决定模型会不会又去搜一遍活动
        self.assertIn('不要再去搜一遍', text)

    def test_pinned_context_empty_without_pin_or_for_foreign_owner(self):
        self.assertEqual(self.conv.pinned_context(), '')
        self.conv.pin_activity = self.foreign
        self.assertEqual(self.conv.pinned_context(), '',
                         '归属不符时不得把别人的活动递到当前用户嘴里')

    def test_send_injects_pin_into_prompt_but_not_into_history(self):
        self.conv.pin_activity = self.activity
        self.conv.save(update_fields=['pin_activity', 'updated_at'])   # 视图重查 DB，不存等于没钉
        service = FakeQoderService()
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.post(f'/chat/{self.conv.id}/send/',
                                    {'content': '还剩多少预算', 'page_context': 'home'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.turn_prompt.startswith('[钉选对象]'),
                        '钉选是用户显式点名的，必须排在按关键词猜的知识库注入之前')
        self.assertIn('预算：¥20000.00', self.conv.turn_prompt)
        self.assertIn('还剩多少预算', self.conv.turn_prompt)
        # 用户消息只存原文（现有约定）：钉选漏进历史会让回看变成一堆方括号
        self.assertEqual(self.conv.messages.filter(role='user').first().content,
                         '还剩多少预算')

    # ── 端点 ──

    def test_pin_sets_then_clears(self):
        resp = self.client.post(self.pin_url, {'activity_id': self.activity.id},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['pin']['name'], '新西兰旅游')
        self.assertIn('data-pin-clear', resp.json()['html'])
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.pin_activity_id, self.activity.id)

        cleared = self.client.post(self.pin_url, {'activity_id': ''},
                                   HTTP_ACCEPT='application/json')
        self.assertIsNone(cleared.json()['pin'])
        self.assertIn('输入', cleared.json()['html'], '取消后回到提示态')
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.pin_activity_id)

    def test_pin_rejects_foreign_activity_as_json_not_html(self):
        """越权必须是 JSON 404：get_visible 抛 Http404 的话 fetch 拿回来的是 HTML 错误页，
        前端 r.json() 直接抛异常，用户只看到「钉选失败」而不知道为什么"""
        resp = self.client.post(self.pin_url, {'activity_id': self.foreign.id},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.pin_activity_id)

    def test_pin_rejects_non_numeric_id(self):
        resp = self.client.post(self.pin_url, {'activity_id': 'abc'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_pin_without_json_accept_redirects_back(self):
        """无 JS 提交（表单/裸 POST）不能把一串 JSON 丢给用户"""
        resp = self.client.post(self.pin_url, {'activity_id': self.activity.id})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/chat/{self.conv.id}/', resp['Location'])

    def test_candidates_search_scopes_to_visible_and_caps(self):
        from activities.models import Activity
        for i in range(8):
            Activity.objects.create(user=self.user, name=f'其他{i}', status='planned')
        found = self.client.get('/chat/pin/search/', {'q': '新西兰'}).json()['candidates']
        self.assertEqual([c['name'] for c in found], ['新西兰旅游'])
        self.assertIn('计划', found[0]['meta'])

        capped = self.client.get('/chat/pin/search/', {'q': ''}).json()['candidates']
        self.assertLessEqual(len(capped), 6, '候选浮层一屏放不下就不要装多')

    def test_candidates_match_tags_too(self):
        """用户记得的是「新西兰」而不是活动全名，所以标签也参与匹配"""
        from activities.models import Activity
        trip = Activity.objects.create(user=self.user, name='10 月出行', status='planned')
        trip.tags.add('新西兰')
        names = [c['name'] for c in
                 self.client.get('/chat/pin/search/', {'q': '新西兰'}).json()['candidates']]
        self.assertIn('10 月出行', names)
        self.assertIn('新西兰旅游', names)     # 命中标签的与命中描述的都要在

    def test_candidates_hide_other_users_activities(self):
        names = [c['name'] for c in
                 self.client.get('/chat/pin/search/', {'q': '活动'}).json()['candidates']]
        self.assertNotIn('别人的活动', names)

    def test_candidates_only_list_active_ones_when_query_is_empty(self):
        from activities.models import Activity
        Activity.objects.create(user=self.user, name='已经办完的事', status='done')
        names = [c['name'] for c in
                 self.client.get('/chat/pin/search/', {'q': ''}).json()['candidates']]
        self.assertIn('新西兰旅游', names)
        self.assertNotIn('已经办完的事', names, '空 @ 时给「还在办的」，不是全部历史')

    def test_chat_page_renders_without_template_syntax_leaks(self):
        """对话页的模板语法泄漏锁

        输入区现在挂了三个 include（pin_host / pin_bar / quick_chips），而浮窗在
        base.html 里 → 漏写一个跨行的 {# #} 就会泄到**每一页**，但现有的泄漏锁
        只盖住活动详情与 Daily（本轮实测就是它先抱住的）。
        """
        self.conv.pin_activity = self.activity
        self.conv.save(update_fields=['pin_activity', 'updated_at'])
        html = self.client.get(f'/chat/{self.conv.id}/').content.decode()
        for token in ('{%', '{{', '{#'):
            self.assertNotIn(token, html, f'渲染结果里出现 {token}，模板语法泄漏')
        # 顺带确认真的渲染进了页面而不是断言一个空范围（空跑锁本项目踩过）
        self.assertIn('data-pin-clear', html)
        self.assertIn('新西兰旅游', html)


class ChatPinWiringTest(SimpleTestCase):
    """钉选的前端接线：两个宿主各一份、状态随历史片段带出、不拼 HTML 字符串"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        js = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.js_code = ChatTurnFlowJsTest._strip_js_comments(js)
        cls.base = (root / 'templates' / 'base.html').read_text(encoding='utf-8')
        cls.detail = (root / 'templates' / 'chat' / 'conversation_detail.html')\
            .read_text(encoding='utf-8')
        cls.widget = (root / 'templates' / 'chat' / 'partials' / 'widget_messages.html')\
            .read_text(encoding='utf-8')
        cls.css = (root / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')

    def test_both_surfaces_mount_a_host_with_distinct_ids(self):
        self.assertIn('pin_host.html', self.detail)
        self.assertIn('pin_host.html', self.base)
        ids = re.findall(r'host_id="([^"]+)"', self.detail + self.base)
        self.assertEqual(len(ids), 2, '两个宿主各挂一份，多了就是写了第三处')
        self.assertEqual(len(set(ids)), 2, 'id 撞了 getElementById 只会拿到第一个')
        for src, name in ((self.detail, '详情页'), (self.base, '浮窗')):
            self.assertIn('window.PaChatPin({', src, '%s 没有初始化钉选交互' % name)

    def test_panel_host_does_not_inherit_the_page_conversation(self):
        """base.html 的浮窗在详情页里渲染时上下文带着 conversation，不显式清空
        会把详情页的钉选状态画进浮窗的槽里（两个对话串数据）"""
        self.assertIn('pin_host.html" with host_id="chat-panel-pin" conversation=None',
                      self.base)

    def test_pin_state_travels_with_the_history_fragment(self):
        """切对话时钉选状态必须跟着换：没钉也要带出隐藏位，否则槽里留着上一个对话的 chip

        搬运发生在宿主页（base.html 的 openConversation），不在 chat-turn.js 里：
        PaChatPin 只供一个 paint()，所以断言要分别看两个文件。"""
        self.assertIn('pin_bar.html', self.widget)
        self.assertIn('mount=True', self.widget)
        self.assertIn('[data-pin-mount]', self.base)
        self.assertIn('paPanelPin.paint(', self.base)

    def test_candidate_list_is_built_with_text_content_not_html_strings(self):
        """活动名是用户数据：用 innerHTML 拼字符串等于给自己埋 XSS（与 Markdown
        渲染器「先转义后解析」同一个道理，这里干脆不生成 HTML 字符串）"""
        self.assertIn('name.textContent = it.name', self.js_code)
        self.assertNotIn('innerHTML = items.map', self.js_code)
        self.assertNotIn('hx-', self.js_code, 'JSON 端点严禁 hx-*')

    def test_pin_set_goes_through_the_shared_json_outlet(self):
        block = self.js_code[self.js_code.index('function setActivity'):
                             self.js_code.index("input.addEventListener('input'")]
        self.assertGreater(len(block.strip()), 300, '切片切空了，这条锁就是假的')
        self.assertIn('apiFetch(u.set', block, '钉选写操作必须走共用 JSON 出口')
        self.assertIn('X-CSRFToken', block)

    def test_pin_css_rules_exist(self):
        for cls in ('.pin-host', '.pin-bar', '.pin-chip', '.pin-candidates', '.pin-item'):
            self.assertIn(cls, self.css)
        self.assertEqual(self.css.count('{'), self.css.count('}'), '花括号不配对')


class ChatFollowUpRenderTest(TestCase):
    """「下一步」chips 的落库与渲染：payload 里有就出，没有就不出"""

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_f', agent_id='ag_f', title='下一步')

    def _fragment(self, content, payload=None):
        from django.template.loader import render_to_string
        msg = Message.objects.create(conversation=self.conv, role='assistant',
                                     content=content, payload=payload)
        return render_to_string('chat/partials/_message.html', {'msg': msg})

    def test_chips_render_with_the_prompt_carried_on_the_button(self):
        html = self._fragment('正文', {'card': '', 'activity_ids': [],
                                      'follow_ups': ['查上海金店报价', '看看本周安排']})
        self.assertIn('data-followup="查上海金店报价"', html)
        self.assertIn('data-followup="看看本周安排"', html)
        self.assertIn('class="follow-ups"', html)

    def test_no_chips_block_without_follow_ups(self):
        html = self._fragment('正文')
        self.assertNotIn('data-followup', html)
        self.assertNotIn('follow-ups', html)

    def test_chips_survive_together_with_a_card(self):
        """卡片与 chips 不互斥：payload 同时带 card 与 follow_ups 时两个都要在"""
        html = self._fragment('正文', {'card': 'activity_list',
                                      'card_data': {'items': [], 'title': '活动'},
                                      'activity_ids': [],
                                      'follow_ups': ['把结论存成文章']})
        self.assertIn('data-followup="把结论存成文章"', html)

    def test_finalized_message_keeps_chips_in_the_payload(self):
        """走完整轮：AI 文本末尾的「下一步」要落到 payload，正文里不留原始标记"""
        service = FakeQoderService(poll_script=[
            {'state': 'ready', 'text': '查完了。\n下一步：看看本周安排｜查上海金店报价'}])
        self.conv.turn_state = Conversation.TURN_AWAITING
        self.conv.turn_started_at = timezone.now()
        self.conv.save()
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.get(f'/chat/{self.conv.id}/turn/', HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        msg = self.conv.messages.filter(role='assistant').first()
        self.assertEqual(msg.content, '查完了。')
        self.assertEqual(msg.payload['follow_ups'], ['看看本周安排', '查上海金店报价'])
        self.assertIn('data-followup="看看本周安排"', resp.json()['html'])

    def test_tool_reply_cannot_swallow_the_models_next_steps(self):
        """工具的 reply 会顶掉模型自己写的那段（「下一步」行就在里面）

        真机实测：钉选后问「还剩多少预算」，模型走 get/query 工具，正文用工具文案，
        chips 随之消失 —— 而最该给下一步的恰好是这些场景，所以要从模型原文里捡回来。
        """
        from core.agent_registry import orchestrator
        from activities.models import Activity
        Activity.objects.create(user=self.user, name='新西兰旅游', status='planned')
        text = ('{"intent": "query", "params": {"name": "新西兰"}, '
                '"reply": "模型的开场白。\\n下一步：看看这个活动的费用明细｜把它改成进行中"}')
        content, payload, _ = orchestrator.process(self.user, text)
        self.assertNotIn('模型的开场白', content, '工具文案应顶掉模型开场白（现有口径）')
        self.assertEqual(payload['follow_ups'], ['看看这个活动的费用明细', '把它改成进行中'])

    def test_js_sends_the_clicked_prompt(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        code = ChatTurnFlowJsTest._strip_js_comments(js)
        self.assertIn("[data-followup]", code)
        self.assertIn("send(follow.getAttribute('data-followup'));", code)

    def test_follow_up_css_exists(self):
        root = Path(__file__).resolve().parent.parent
        css = (root / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')
        self.assertIn('.follow-ups', css)
        self.assertIn('.follow-ups-label', css)


# chat/urls.py 里「由原生 fetch 消费」的端点（AGENTS.md 双协议条）。
# 这张表是唯一清单：新增 fetch 端点必须同时加在这里，否则下面两条锁会报出来。
JSON_FETCH_ENDPOINTS = {
    'create_conversation': ('post', '/chat/create/'),
    'send_message': ('post', '/chat/1/send/'),
    'turn_poll': ('get', '/chat/1/turn/'),
    'turn_cancel': ('post', '/chat/1/turn/cancel/'),
    'pin_conversation': ('post', '/chat/1/pin/'),
    'pin_candidates': ('get', '/chat/pin/search/'),
}


class ChatAuthExpiryTest(TestCase):
    """登录态过期时，fetch 端点必须回 401 JSON 而不是 302 到登录页

    2026-09-02 线上冒烟实测：未登录 POST /chat/22/pin/ 返回 302
    /accounts/login/?next=/chat/22/pin/，fetch 会自动跟随重定向，最终拿到 200 的
    登录页 HTML → JSON 解析失败 → 前端只能报「钉选失败，请重试」，用户反复重试也不
    知道是掉线。收口见 core.utils.json_login_required。

    不管 403：CSRF Cookie 与 Session 不同期（默认一年），常见情形是 Session 过期而
    CSRF 仍有效，能走到鉴权分支拿到 401；真拿到 403（CSRF 也没了）说明页面本身该刷新，
    不靠这个分支兼顶。
    """

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')

    def _hit(self, method, url, **extra):
        return getattr(self.client, method)(url, HTTP_ACCEPT='application/json', **extra)

    def test_signed_out_fetch_gets_json_401_on_every_endpoint(self):
        for name, (method, url) in JSON_FETCH_ENDPOINTS.items():
            with self.subTest(endpoint=name):
                resp = self._hit(method, url)
                self.assertEqual(resp.status_code, 401,
                                 f'{name} 未登录时应回 401，实际 {resp.status_code}')
                self.assertEqual(resp['Content-Type'], 'application/json')
                body = resp.json()
                self.assertTrue(body.get('login_url'), '前端靠这个字段做整页跳转')
                self.assertIn('登录已过期', body['error'])

    def test_login_url_keeps_the_next_pointer(self):
        """跳转地址必须带着 next 指回原页面（含查询串），登录后一步回到原地

        只用 ASCII 查询串：测试客户端对非 ASCII 的编码路径与真实浏览器不同，
        拿中文断言 percent-encoding 会碎在双重编码上（实测）—— 假失败比没锁更坏。
        真正要钉住的是「查询串没被丢弃」：? 在 next 里必须被转义成 %3F。
        """
        body = self._hit('get', '/chat/pin/search/?q=huangshan').json()
        self.assertIn('next=', body['login_url'])
        self.assertIn('%3Fq%3Dhuangshan', body['login_url'],
                      '查询串必须整体编进 next，不能因截断而丢失（%3F = ?）')

    def test_signed_out_plain_request_still_redirects(self):
        """整页表单（无 Accept）保持 Django 原生 302：不破坏无 JS 降级"""
        resp = self.client.get('/chat/pin/search/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp['Location'])

    def test_htmx_requests_are_not_turned_into_401(self):
        """HTMX 侧口径不动：它的 Accept 是 text/html,*/*，仍然命中 302 分支

        为什么不顺手也改掉：htmx 对非 2xx 的处理与 fetch 不同，换 401 要连带验证
        HX-Redirect 在错误响应上到底会不会被处理，属另一票改动。
        """
        resp = self.client.get('/chat/pin/search/', HTTP_ACCEPT='text/html,*/*',
                               HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 302)

    def test_signed_in_json_endpoints_still_answer_json(self):
        """装饰器是 @login_required 的超集：登录后行为一个字都不能变"""
        self.client.force_login(self.user)
        resp = self.client.get('/chat/pin/search/', HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/json')
        self.assertIn('candidates', resp.json())


class ChatAuthExpiryWiringTest(SimpleTestCase):
    """收口的结构锁：出口唯一、宿主复用、没有任何聊天端点裸奔

    这一组不测行为测形状：行为只能证明写到的那几个 case，而「以后新加的端点忘了分类」
    只有遍历路由表 + 扫源码才抗得住。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        js = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.js_code = ChatTurnFlowJsTest._strip_js_comments(js)
        cls.base = (root / 'templates' / 'base.html').read_text(encoding='utf-8')

    def test_only_the_shared_outlet_uses_raw_fetch(self):
        """只允许 apiFetch 内部那一处裸 fetch；第二处就是同时丢了 Accept 与 401 处理

        计数用剔了注释的代码，且只匹小写 fetch(：apiFetch(/paJsonFetch 都是大写 F，
        不会被算进来（这条判据能成立全靠命名区分得开）。
        """
        hits = re.findall(r'(?<![\w$])fetch\(', self.js_code)
        self.assertEqual(len(hits), 1, f'裸 fetch 出现 {len(hits)} 处，只允许出口内部 1 处')

    def test_outlet_reads_the_server_provided_login_url(self):
        block = self.js_code[self.js_code.index('function apiFetch'):
                             self.js_code.index('window.paJsonFetch')]
        self.assertGreater(len(block.strip()), 300, '切片切空了，这条锁就是假的')
        self.assertIn('r.status !== 401', block)
        self.assertIn('d.login_url', block, '跳转地址由服务端拼好，前端不自己拼 URL')
        # 断言到「闸门真在判断里」这一层：只断 redirecting 存在是假的，
        # 删掉条件但留着 redirecting = true 赋值时照样能过（变异反证当场拆穿）
        self.assertIn('|| redirecting) return r', block,
                      '轮询可能连着好几拍 401，跳转必须被闸门挡住第二次')
        self.assertIn("headers['Accept'] = 'application/json'", block)
        # next 必须被改写成当前页：服务端给的 next 是接口地址，登录后会看到一坨 JSON
        # （真机实测发现），而只断 d.login_url 存在留不住这个口径
        self.assertIn("searchParams.set('next', here)", block,
                      '跳转要带页内地址，不能直接把接口 URL 当 next')
        self.assertIn('location.pathname + location.search', block)
        # 401 后绝不把响应交回调用链：否则调用链会再报一次「发送/创建失败」
        # （真机实测：新建对话闪 alert），所以上面返的是永不 resolve 的 Promise
        self.assertEqual(block.count('return new Promise(function () {});'), 2,
                         '两个 401 分支都要抹掉后续链路（JSON 成功分支与解析失败分支）')

    def test_host_page_uses_the_same_outlet_for_create(self):
        """base.html 的「+ 新对话」以前靠假的 HX-Request 头骗视图返 JSON

        那样未登录时装饰器会按 HTML 请求返 302，401 分支永远走不到（所以不只是一个
        雅观问题）。
        """
        from core.layout_asserts import code_only
        # 两道都得剔：code_only 只剔 HTML/{% comment %} 注释，而这段说明写在内联
        # JS 的 // 注释里 —— 不剔就会把「旧的骗法不得回来」的说明当成违规（本项目老坑）
        code = ChatTurnFlowJsTest._strip_js_comments(code_only(self.base))
        self.assertIn("paJsonFetch('/chat/create/'", code)
        self.assertNotIn("'HX-Request': 'true'", code, '旧的骗法不得回来')
        self.assertLess(self.base.index('chat-turn.js'), self.base.index('paJsonFetch'),
                        'chat-turn.js 必须先于内联脚本加载，否则 paJsonFetch 是 undefined')

    def test_no_chat_endpoint_is_public(self):
        """遍历路由：每个聊天端点未登录时要么 302 要么 401，绝不真进视图

        故意不用装饰器属性做判据（login_required 不暴露任何标记，只能靠行为）；
        而 SimpleTestCase 不开放数据库，一旦某个端点漏了登录保护，视图里的 ORM
        查询直接抛 DatabaseAccessError，比断言属性更真。
        """
        from chat import urls as chat_urls

        for pattern in chat_urls.urlpatterns:
            raw = str(pattern.pattern)
            url = '/chat/' + re.sub(r'<int:[\w_]+>', '1', raw)
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertIn(resp.status_code, (302, 401),
                              f'{url} 未登录时返回 {resp.status_code}，看起来没有登录保护')

    def test_json_endpoints_are_decorated_and_the_list_is_exhaustive(self):
        """路由表里的端点必须被完整分类：fetch 端点带 json_login_required，
        其余端点不得内联返回 JsonResponse（否则它就是漏登记的 fetch 端点）"""
        import inspect
        from chat import urls as chat_urls

        marked, problems = set(), []
        for pattern in chat_urls.urlpatterns:
            cb = pattern.callback
            name = getattr(cb, '__name__', str(cb))
            if getattr(cb, 'json_login_required', False):
                marked.add(name)
                continue
            try:
                src = inspect.getsource(cb)   # inspect 会跟 __wrapped__ 取到原视图体
            except OSError:
                continue
            if 'JsonResponse' in src:
                problems.append(f'{name}: 返回 JsonResponse 却不在 JSON_FETCH_ENDPOINTS 清单里')
        self.assertEqual(sorted(marked), sorted(JSON_FETCH_ENDPOINTS),
                         '装饰器套的端点与清单不一致（多套 = 白套，少套 = 登录过期会回 302）')
        self.assertEqual(problems, [], '\n'.join(problems))


class ConversationRenameDeleteTest(TestCase):
    """AI 对话的改名 / 删除：权限隔离 + 清理顺序 + 空 title 不改"""

    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user('alice', password='p')
        self.bob = User.objects.create_user('bob', password='p')
        self.conv = Conversation.objects.create(
            user=self.alice, session_id='sess-1', title='原标题',
        )
        Message.objects.create(conversation=self.conv, role='user', content='hi')

    # --- rename ---
    def test_rename_updates_title_and_redirects_to_detail(self):
        self.client.force_login(self.alice)
        resp = self.client.post(
            reverse('chat:conversation_rename', args=[self.conv.id]),
            {'title': '新标题'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/chat/{self.conv.id}/', resp.url)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.title, '新标题')

    def test_rename_with_blank_title_keeps_original(self):
        """空串 / 纯空白视为「取消」，不得把标题改成空"""
        self.client.force_login(self.alice)
        for blank in ('', '   ', '\t'):
            with self.subTest(blank=repr(blank)):
                self.client.post(
                    reverse('chat:conversation_rename', args=[self.conv.id]),
                    {'title': blank},
                )
                self.conv.refresh_from_db()
                self.assertEqual(self.conv.title, '原标题')

    def test_rename_truncates_overlong_title(self):
        self.client.force_login(self.alice)
        self.client.post(
            reverse('chat:conversation_rename', args=[self.conv.id]),
            {'title': 'A' * 500},
        )
        self.conv.refresh_from_db()
        self.assertEqual(len(self.conv.title), 255)

    def test_rename_other_users_conversation_returns_404(self):
        """权限隔离：bob 改 alice 的对话必须 404，不能靠改 URL 里的 id 越权"""
        self.client.force_login(self.bob)
        resp = self.client.post(
            reverse('chat:conversation_rename', args=[self.conv.id]),
            {'title': '被篡改'},
        )
        self.assertEqual(resp.status_code, 404)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.title, '原标题')

    # --- delete ---
    def test_delete_removes_conversation_and_messages(self):
        self.client.force_login(self.alice)
        conv_id, msg_count = self.conv.id, self.conv.messages.count()
        self.assertGreater(msg_count, 0)
        resp = self.client.post(reverse('chat:conversation_delete', args=[conv_id]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('chat:conversation_list'))
        self.assertFalse(Conversation.objects.filter(id=conv_id).exists())
        self.assertEqual(Message.objects.filter(conversation_id=conv_id).count(), 0)

    def test_delete_resets_active_turn_before_deleting(self):
        """turn 还在跑时删除必须先 reset_turn，否则状态机残留"""
        self.conv.turn_state = 'awaiting'
        self.conv.turn_prompt = 'pending text'
        self.conv.save(update_fields=['turn_state', 'turn_prompt'])

        reset_called = []
        original = Conversation.reset_turn

        def spy_reset(self_conv):
            reset_called.append(True)
            return original(self_conv)

        self.client.force_login(self.alice)
        with patch.object(Conversation, 'reset_turn', spy_reset):
            self.client.post(reverse('chat:conversation_delete', args=[self.conv.id]))
        self.assertTrue(reset_called, 'delete 必须先调 reset_turn')

    def test_delete_other_users_conversation_returns_404(self):
        self.client.force_login(self.bob)
        resp = self.client.post(reverse('chat:conversation_delete', args=[self.conv.id]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Conversation.objects.filter(id=self.conv.id).exists())

    def test_delete_requires_post(self):
        """GET 不得触发删除（防爬虫 / 预加载误伤）"""
        self.client.force_login(self.alice)
        resp = self.client.get(reverse('chat:conversation_delete', args=[self.conv.id]))
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(Conversation.objects.filter(id=self.conv.id).exists())
