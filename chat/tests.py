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
        start = self.flow.index("fetch(u.send")
        return self.flow[start:self.flow.index('})', start)]

    def test_send_fetch_asks_for_json(self):
        """视图靠 Accept 区分 fetch 与无 JS 提交；漏了会被 302 重定向，前端拿到 HTML"""
        self.assertIn("'Accept': 'application/json'", self._send_fetch_block(),
                      '发送请求必须显式要 JSON')

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
        self.assertIn("'Accept': 'application/json'", body, 'cancel 也是 JSON 端点')
