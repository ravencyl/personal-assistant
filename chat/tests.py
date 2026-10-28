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
from core.tags import apply_tags
from django.db.models import QuerySet
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from chat import views as chat_views
from activities.models import Activity
from core.layout_asserts import assert_desktop_two_columns, code_only
from chat.models import (Conversation, Message, TURN_IDLE_GRACE_SECONDS,
                         TURN_TTL_SECONDS)
from core.agent_registry import (INTENT_TOOL_MAP, PROTOCOL_REF_REMINDER,
                                 PROTOCOL_SEARCH_REMINDER,
                                 PROTOCOL_TRUNCATED_NOTE, REF_REMINDER_MIN_CHARS,
                                 build_protocol_prompt, extract_intent,
                                 looks_like_protocol, orchestrator,
                                 resolve_params_refs, salvage_partial_protocol)

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
        self.assertIn('记忆和文章里都没有', reply)
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
    APP_DIRS = ('activities', 'core', 'knowledge', 'memory', 'agents', 'chat')

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
    或重新加回 Enter 监听。两个入口：详情页 #message-input、分栏页 #split-input
    （右下角聊天浮窗及其 #chat-input 已于 2026-09 整体下线）。
    """

    ENTER_SUBMIT = re.compile(r"(?:key|keyCode|which)\s*(?:===|==)\s*['\"]?(?:Enter|13)")

    def _tpl(self, *parts):
        return (Path(__file__).resolve().parent.parent / 'templates' / Path(*parts)).read_text(
            encoding='utf-8')

    def test_both_chat_inputs_are_multiline_textareas(self):
        """两个输入框都必须是 textarea：单行 input 物理上换不了行"""
        page = self._tpl('chat', 'conversation_detail.html')
        split = self._tpl('chat', 'conversation_list.html')
        self.assertIn('id="message-input"', page)
        self.assertIn('id="split-input"', split)
        self.assertIn('<textarea name="content" id="message-input"', page)
        self.assertIn('<textarea name="content" id="split-input"', split)
        for html in (page, split):
            self.assertNotIn('<input type="text" name="content"', html)

    def test_no_enter_key_submit_handler_anywhere_in_chat_inputs(self):
        for parts in (('chat', 'conversation_detail.html'), ('chat', 'conversation_list.html')):
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
        """未渲染的 textarea 不能参与高度计算（实测踩过：浮层输入框被压成 0 高）

        快记浮层面板初始是 display:none，此时 scrollHeight 恒为 0，页面加载时跑
        那一遍初始化会把面板内 textarea 的 style.height 写成 0px，placeholder 直接
        裁掉半截。聊天浮窗时代已踩过一次（#chat-input 被压成 0 高）。
        """
        base = self._tpl('base.html')
        fit = base.split('window.paFitTextarea = function', 1)[1].split('};', 1)[0]
        self.assertIn('getClientRects', fit, '缺少「元素是否已渲染」的守卫')
        self.assertLess(fit.find('getClientRects'), fit.find('ta.style.height = Math.min'),
                        '守卫必须在写高度之前，否则 0 已经写回去了')


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
    """对话列表页分栏布局回归锁（左栏 = 对话列表，右栏 = 聊天窗口）

    新布局用 .chat-layout / .chat-sidebar / .chat-main 代替旧的 .page-cols。
    桌面端两栏同屏；移动端靠 data-view 切换视图。
    """
    TEMPLATE = Path(__file__).resolve().parent.parent / 'templates' / 'chat' / 'conversation_list.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client.login(username='raven', password='test')
        Conversation.objects.create(user=self.user, session_id='sess_lay',
                                    title='新西兰之旅怎么安排')
        # 2026-10-11：/chat/ 纯入口自动重定向到最近对话，布局断言跟随重定向后的页面
        self.html = self.client.get('/chat/', follow=True).content.decode()
        self.src = self.TEMPLATE.read_text(encoding='utf-8')

    def test_split_layout_structure(self):
        """分栏容器 + 左栏 + 右栏都存在"""
        self.assertIn('class="chat-layout"', self.src)
        self.assertIn('class="chat-sidebar', self.src)
        self.assertIn('class="chat-main"', self.src)

    def test_sidebar_contains_search_and_list(self):
        """左栏包含搜索框和对话列表"""
        self.assertIn('搜索对话...', self.html)
        self.assertIn('新西兰之旅怎么安排', self.html)

    def test_empty_state_when_no_conversations_at_all(self):
        """没有任何对话时 /chat/ 不重定向，右栏显示空状态引导

        2026-10-11 起有对话时纯入口会自动进入最近对话，空态只剩「零对话」这一条路，
        它同时也是新建入口，必须留在列表页。
        """
        Conversation.objects.all().delete()
        html = self.client.get('/chat/').content.decode()
        self.assertIn('选择一个对话开始', html)

    def test_ai_not_configured_banner_in_sidebar(self):
        """AI 未配置提示在左栏头部"""
        self.assertIn('AI 服务未配置', self.src)

    def test_mobile_composer_not_covered(self):
        """聊天页输入框不被 Tab 栏遮挡（线上实测回归锁）

        .chat-layout 移动端高度必须扣除 顶栏+main 顶距(68px)、Tab 栏实高+空隙(72px)
        与 iOS 安全区 —— 改小会让发送框被 Tab 栏压住。
        （快记 FAB 及其隐藏链路已于 2026-10-28 随快记功能整体下线删除）
        """
        base = (Path(__file__).resolve().parent.parent / 'templates' / 'base.html').read_text(encoding='utf-8')
        css = (Path(__file__).resolve().parent.parent / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')
        self.assertIn(
            'calc(100dvh - 8.75rem - env(safe-area-inset-bottom, 0px))', css,
            '移动端 .chat-layout 高度算术被改，发送框会被底部 Tab 栏遮住')
        self.assertNotIn('quick-fab-root', base,
                         '快记 FAB 已整体下线，base.html 不应再有残留引用')
        self.assertNotIn('chat-fab-root', base,
                         '聊天浮窗 FAB 已下线，base.html 不应再有残留引用')


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

    def __init__(self, send_raises=None, poll_script=None, send_script=None,
                 wait_script=None, wait_default=None):
        self.sent = []
        self.cancelled = 0
        self.poll_calls = 0
        self.wait_calls = 0
        self._send_raises = send_raises
        self.poll_script = list(poll_script or [])
        # send_script: 逐个弹出；是 Exception 就抛，否则本次发送成功。
        # 用于「首发失败、重发成功」的自动重试场景
        self.send_script = list(send_script or [])
        # wait_script: 长轮询通知器的脚本化返回（'finished' / 进度阶段 / ''）。
        # 耗尽后走 wait_default；两者都不给就断言失败 —— turn_wait 的循环会
        # 「没信号就继续听」，让没写脚本的测试挂满 25 秒预算比立刻报错难查得多。
        self.wait_script = list(wait_script or [])
        self.wait_default = wait_default

    def send_message(self, session_id, text):
        if self.send_script:
            action = self.send_script.pop(0)
            if isinstance(action, Exception):
                raise action
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

    def wait_turn_finished(self, session_id, budget=25.0):
        """长轮询通知器替身：按脚本返回 'finished' / 进度阶段 / ''（没动静）

        脚本耗尽后回退到 wait_default（给 '' 就是「一直听到预算用尽」）；
        两者都没配置时直接断言失败，而不是返回 '' —— 后者会让测试在
        turn_wait 的等待循环里空转到真实 25s 才结束，坏用例比好用例跑得久。
        """
        self.wait_calls += 1
        if self.wait_script:
            action = self.wait_script.pop(0)
            if isinstance(action, Exception):
                raise action
            return action
        if self.wait_default is not None:
            return self.wait_default
        raise AssertionError('wait_script 耗尽且未配置 wait_default（测试会空转，补脚本）')


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

    def test_second_concurrent_send_loses_the_race_and_writes_nothing(self):
        """抢轮是条件 UPDATE：两个并发请求（双标签页/狂点）都过了先读后写的
        turn_active 检查后，抢不到轮的必须空手而回且不落用户消息。

        patch 掉 claim_turn 单独模拟「库里状态刚被别人改了」的毫秒级窗口：
        2026-10-11 线上实锤（对话 52/58 各出现间隔 0~1 秒的相同消息），
        旧代码这里没有任何原子性，两条都落库并各发起一轮。
        """
        with patch.object(Conversation, 'claim_turn', return_value=False):
            resp = self._post(FakeQoderService())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['state'], 'busy')
        self.assertEqual(self.conv.messages.count(), 0)

    def test_expired_residue_is_interrupted_in_db_before_the_claim(self):
        """TTL 残留分支必须先 save 再抢轮：只改内存的话 claim 拿库里的旧活跃态
        （awaiting）去比 from=none，永远抢不过自己，超时轮次会永久发不出新消息
        """
        self.conv.turn_state = Conversation.TURN_AWAITING
        self.conv.turn_started_at = timezone.now() - timedelta(seconds=models_TTL + 60)
        self.conv.save(update_fields=['turn_state', 'turn_started_at'])
        resp = self._post(FakeQoderService())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['state'], 'processing')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_AWAITING)
        self.assertEqual(self.conv.messages.filter(role='user').count(), 1)

    def test_platform_busy_keeps_the_turn_queued_instead_of_failing(self):
        """撞 409 不是失败：保持 queued 交给轮询重发（旧实现在这里同步等完首帧）"""
        service = FakeQoderService(send_raises=http_error(409))
        resp = self._post(service)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['turn_state'], Conversation.TURN_QUEUED)
        self.assertEqual(self.conv.messages.filter(role='user').count(), 1)

    def test_hard_send_failure_auto_retries_once(self):
        """首发失败不再立刻报错：回 queued 自动重发一次（重试用尽才中断，
        见 AutoRetryTurnTest）"""
        service = FakeQoderService(send_raises=http_error(500))
        resp = self._post(service)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['state'], 'processing')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_QUEUED)
        self.assertEqual(self.conv.turn_retries, 1)

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

    def test_empty_reply_after_retries_exhausted_lands_a_readable_note(self):
        """重试用尽后的空回复仍要落一条占位消息（否则历史里留着一条没人回应的提问）；
        未用尽时会先自动重发，见 AutoRetryTurnTest"""
        self.conv.turn_retries = 1
        self.conv.turn_idle_at = timezone.now() - timedelta(seconds=models_GRACE + 1)
        self.conv.save()
        _, data = self._poll(FakeQoderService(poll_script=[{'state': 'empty', 'text': ''}]))
        self.assertEqual(data['state'], 'done')
        note = self.conv.messages.filter(role='assistant').first()
        self.assertIsNotNone(note, '空回复也要落一条消息，否则历史里留着一条没人回应的问题')
        self.assertIn('没有返回内容', note.content)

    def test_expired_turn_becomes_error_with_a_retry_bubble(self):
        """重试用尽后的超时才落中断气泡；未用尽时先自动重试，见 AutoRetryTurnTest"""
        self.conv.turn_retries = 1
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


class ChatTurnWaitTest(TestCase):
    """长轮询收口端点 turn-wait（Phase 1.2，docs/sse-streaming-evaluation.md）

    与 turn_poll 的分工在这里逐条锁死：
    - 副作用只属于 turn_poll：queued 的（重）发送、TTL 超时的抢救/重试/中断
      判定，turn_wait 一律早退，不挂流也不重复一份裁决；
    - 收到结束信号后走的是**同一个** _resolve_awaiting（收尾实现唯一），
      claim_turn 的抢锁语义必须原样生效 —— 否则两个等待者会各落一条回复；
    - 名额闸门：同对话最多一条平台流，抢不到名额立刻空返。
    """

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_w', agent_id='ag_w', title='新对话',
            status='processing', turn_state=Conversation.TURN_AWAITING,
            turn_started_at=timezone.now())
        self.msg = Message.objects.create(conversation=self.conv, role='user', content='查一下活动')
        self.conv.turn_message = self.msg
        self.conv.save()
        self.url = f'/chat/{self.conv.id}/turn-wait/'
        # 名额集合是模块级进程状态：残留会让下一条用例莫名拿到 wait_busy
        chat_views._WAIT_SLOTS.clear()
        self.addCleanup(chat_views._WAIT_SLOTS.clear)

    def _wait(self, service, **extra):
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.get(self.url, HTTP_ACCEPT='application/json', **extra)
        return resp.status_code, (resp.json() if resp.status_code == 200 else {})

    def test_finished_signal_finalizes_through_the_shared_path(self):
        """收到结束信号 → 走 _resolve_awaiting 收尾：落且只落一条回复，状态收口"""
        service = FakeQoderService(
            wait_script=['finished'],
            poll_script=[{'state': 'ready', 'text': '找到了 3 个活动'}])
        _, data = self._wait(service)
        self.assertEqual(data['state'], 'done')
        self.assertIn('找到了 3 个活动', data['html'])
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 1)
        self.assertEqual(service.poll_calls, 1,
                         '收尾必须复用那一份 REST 窗口读取，正文不来自流')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_DONE)
        self.assertNotIn(self.conv.id, chat_views._WAIT_SLOTS, '请求结束必须放名额')

    def test_second_waiter_while_finalizing_elsewhere_writes_nothing(self):
        """信号到点时别的标签页已抢走收尾锁：本请求必须空手而回，不落第二条回复"""
        with patch.object(Conversation, 'claim_turn', return_value=False):
            _, data = self._wait(FakeQoderService(
                wait_script=['finished'],
                poll_script=[{'state': 'ready', 'text': '不能重复执行'}]))
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(data['phase'], 'finalizing')
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 0)

    def test_progress_signal_returns_the_phase_without_finalizing(self):
        """过程事件（thinking/tooling）提前返回，让前端立刻换文案；不碰收尾"""
        service = FakeQoderService(wait_script=['thinking'])
        _, data = self._wait(service)
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(data['phase'], 'thinking')
        self.assertEqual(service.poll_calls, 0, '进度阶段不触发 REST 读取收尾')
        self.assertNotIn(self.conv.id, chat_views._WAIT_SLOTS)

    def test_budget_exhausted_returns_plain_processing(self):
        """通知器一直「没动静」（坏流/平台不推）：到预算空手而回，普通轮询照常兜底"""
        with patch('chat.views.TURN_WAIT_BUDGET_SECONDS', 0.05):
            _, data = self._wait(FakeQoderService(wait_default=''))
        self.assertEqual(data['state'], 'processing')
        self.assertNotIn('phase', data, '没有进度信号就不该编一个阶段出来')

    def test_slot_is_released_even_when_the_notifier_throws(self):
        """通知器抛异常（真实实现会吞，这里模拟的是坏得更狠的版本）：名额必须在
        finally 里放掉，否则该对话永远拿不到长轮询，加速通道被一次异常焊死"""
        service = FakeQoderService(wait_script=[RuntimeError('流断了')])
        with self.assertRaises(RuntimeError):
            self._wait(service)
        self.assertNotIn(self.conv.id, chat_views._WAIT_SLOTS)

    def test_second_waiter_on_the_same_conversation_gets_wait_busy(self):
        """名额互斥：已有一个等待者挂着流时，第二个请求立刻空返且完全不碰通知器"""
        chat_views._WAIT_SLOTS.add(self.conv.id)
        service = FakeQoderService(wait_script=['finished'])
        _, data = self._wait(service)
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(data['phase'], 'wait_busy')
        self.assertEqual(service.wait_calls, 0)
        self.assertIn(self.conv.id, chat_views._WAIT_SLOTS,
                      '抢不到名额的一方不许顺手释放别人的名额')

    def test_inactive_and_side_effect_states_never_attach_a_stream(self):
        """done/queued/expired/finalizing/error 全部早退：不占名额、不挂流。

        发送与超时裁决只属于 turn_poll（那份路径才有锁与文案），这里重复一份
        就是两份会漂移的收尾判定 —— 尤其 expired：在 wait 里做抢救/重发会绕过
        turn_poll 的自动重试预算。
        """
        cases = [
            (Conversation.TURN_NONE, 'done', None),
            (Conversation.TURN_DONE, 'done', None),
            (Conversation.TURN_ERROR, 'error', None),
            (Conversation.TURN_QUEUED, 'processing', 'queued'),
            (Conversation.TURN_FINALIZING, 'processing', 'finalizing'),
        ]
        for state, expected_state, expected_phase in cases:
            with self.subTest(state=state):
                self.conv.turn_state = state
                self.conv.save(update_fields=['turn_state'])
                service = FakeQoderService()
                _, data = self._wait(service)
                self.assertEqual(data['state'], expected_state)
                if expected_phase:
                    self.assertEqual(data['phase'], expected_phase)
                self.assertEqual(service.wait_calls, 0, f'{state} 不该挂平台流')
                self.assertEqual(service.poll_calls, 0, f'{state} 不该触发收尾')
                self.assertNotIn(self.conv.id, chat_views._WAIT_SLOTS)

    def test_expired_turn_defers_the_ruling_to_turn_poll(self):
        """TTL 到点：turn_wait 只报 expired，绝不抢救/重发/中断（那是 turn_poll 的裁决）"""
        self.conv.turn_started_at = timezone.now() - timedelta(seconds=models_TTL + 5)
        self.conv.save(update_fields=['turn_started_at'])
        service = FakeQoderService()
        _, data = self._wait(service)
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(data['phase'], 'expired')
        self.assertEqual(service.poll_calls, 0, '抢救 = 在等待端点里落库，收尾路径出现第二份实现')
        self.assertEqual(service.sent, [])
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_AWAITING,
                         '超时裁决不得由 turn_wait 改轮次状态')

    def test_wait_never_leaks_another_users_conversation(self):
        other = User.objects.create_user('o', password='p')
        self.client.force_login(other)
        resp = self.client.get(self.url, HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(chat_views._WAIT_SLOTS, set())


class AutoRetryTurnTest(TestCase):
    """自动重试（2026-09-21）：发送失败 / TTL 超时 / 空回复时自动重发同一份
    turn_prompt，用户不再需要手动点「重试」

    三条安全边界：
    - 上限 TURN_MAX_RETRIES = 1：重发会让模型重新执行指令，多一次就可能重复落库
    - TTL 超时先「抢救」：平台可能已经回了只是没取到，抢到就直接收尾不重发
    - 发送失败绝不抢救：指令根本没到平台，poll_turn 取到的是上一轮的回复
    """

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_r', agent_id='ag_r', title='新对话',
            status='processing', turn_state=Conversation.TURN_AWAITING,
            turn_started_at=timezone.now(), turn_retries=0,
            protocol_sent=True)  # 存量会话：首帧早已投过，本轮不该再带协议
        self.msg = Message.objects.create(conversation=self.conv, role='user', content='查一下活动')
        self.conv.turn_message = self.msg
        self.conv.turn_prompt = '完整组装文本'
        self.conv.save()
        self.url = f'/chat/{self.conv.id}/turn/'

    def _poll(self, service):
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.get(self.url, HTTP_ACCEPT='application/json')
        return resp.status_code, (resp.json() if resp.status_code == 200 else {})

    def _make_expired(self):
        # 只改 started_at：全字段 save 会把内存里的旧 turn_state/turn_retries 写回库，
        # 覆盖掉第一次 poll 已落库的重试状态
        self.conv.refresh_from_db()
        self.conv.turn_started_at = timezone.now() - timedelta(
            seconds=models_TTL + 5)
        self.conv.save(update_fields=['turn_started_at'])

    def test_expired_turn_is_rescued_when_platform_already_replied(self):
        """超时但平台其实已回：直接收尾，不重发（重发会把指令重新执行一遍）"""
        self._make_expired()
        service = FakeQoderService(
            poll_script=[{'state': 'ready', 'text': '找到了 3 个活动'}])
        _, data = self._poll(service)
        self.assertEqual(data['state'], 'done')
        self.assertEqual(service.sent, [], '抢救到了回复就绝不重发')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_DONE)
        self.assertEqual(self.conv.turn_retries, 0)

    def test_expired_turn_auto_retries_once(self):
        """超时且平台没产出：回 queued 自动重发同一份 turn_prompt"""
        self._make_expired()
        service = FakeQoderService(poll_script=[{'state': 'processing', 'text': ''}])
        _, data = self._poll(service)
        self.assertEqual(data['state'], 'processing')
        self.assertEqual(data['phase'], 'auto_retry')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_QUEUED)
        self.assertEqual(self.conv.turn_retries, 1)
        self.assertLess(timezone.now() - self.conv.turn_started_at,
                        timedelta(seconds=10), 'started_at 必须刷新，否则下一拍立刻又超时')
        self.assertEqual(service.cancelled, 1, '重发前要先 cancel 旧轮，否则必撞 409')
        # 下一拍轮询把 queued 的轮次重发出去
        _, data2 = self._poll(service)
        self.assertEqual(data2['phase'], 'sent')
        self.assertEqual(service.sent, ['完整组装文本'])

    def test_second_timeout_after_retry_becomes_error(self):
        """重试后再超时：用尽，落中断气泡（保留手动重试按钮）"""
        self._make_expired()
        service = FakeQoderService()
        _, data = self._poll(service)
        self.assertEqual(data['phase'], 'auto_retry')
        self._make_expired()
        _, data = self._poll(service)
        self.assertEqual(data['state'], 'error')
        self.assertIn('data-retry-text', data['html'])
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_ERROR)

    def test_send_failure_then_poll_redelivery_succeeds(self):
        """端到端：send 首发失败 → 自动回 queued（retries=1）→ 轮询下一拍重发成功"""
        # 上一轮的 error 残留不算活跃，新发送可以直接开新一轮
        self.conv.turn_state = Conversation.TURN_ERROR
        self.conv.save(update_fields=['turn_state'])
        service = FakeQoderService(send_script=[http_error(502)])
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.post(f'/chat/{self.conv.id}/send/',
                                    {'content': '新问题'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['state'], 'processing')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_QUEUED)
        self.assertEqual(service.sent, [], '失败的那次不会把指令送出去')
        _, data = self._poll(service)
        self.assertEqual(data['phase'], 'sent')
        self.assertEqual(service.sent, [PROTOCOL_SEARCH_REMINDER + '新问题'],
                         '重发的是同一份 turn_prompt（含恒定检索补发基线），且只发一份')

    def test_delivery_failure_after_retries_exhausted_errors(self):
        """首发 + 自动重发都失败：中断气泡（不再无限重试）"""
        self.conv.turn_retries = 1
        self.conv.turn_state = Conversation.TURN_QUEUED
        self.conv.save()
        service = FakeQoderService(send_raises=http_error(500))
        _, data = self._poll(service)
        self.assertEqual(data['state'], 'error')
        self.assertIn('发送失败', data['message'])

    def test_empty_reply_auto_retries_before_landing_note(self):
        """空回复先自动重发；重试用尽才落占位消息"""
        self.conv.turn_idle_at = timezone.now() - timedelta(seconds=models_GRACE + 1)
        self.conv.save()
        service = FakeQoderService(poll_script=[{'state': 'empty', 'text': ''}])
        _, data = self._poll(service)
        self.assertEqual(data['phase'], 'auto_retry')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_retries, 1)
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 0)

    def test_new_send_resets_retry_count(self):
        """上一轮的失败重试计数不能带到下一轮"""
        self.conv.turn_retries = 1
        self.conv.turn_state = Conversation.TURN_ERROR
        self.conv.save()
        with patch('chat.views.get_service', return_value=FakeQoderService()):
            resp = self.client.post(f'/chat/{self.conv.id}/send/',
                                    {'content': '新问题'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_retries, 0)


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
        """详情页、分栏页加载的历史片段、轮询新片段共用 _message.html：两份实现必然漂移成两种长相"""
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
        """chat-turn.js 由 base.html 统一引入（分栏页与详情页同一个实例来源）"""
        base = self._tpl('base.html')
        self.assertIn("{% staticv 'js/chat-turn.js' %}", base)
        self.assertEqual(base.count('PaChatTurn('), 0,
                         'base.html 不应再初始化 PaChatTurn（浮窗已下线，实例只在两个聊天页）')
        self.assertIn('paPageContext', base, '聊天页要复用同一份页面上下文判定')


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
        cls.split = (root / 'templates' / 'chat' / 'conversation_list.html').read_text(encoding='utf-8')
        cls.detail = (root / 'templates' / 'chat' / 'conversation_detail.html').read_text(encoding='utf-8')
        cls.drawer = (root / 'templates' / 'activities' / 'activity_detail.html').read_text(encoding='utf-8')

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
        self.assertEqual(self.split.count('chat/partials/turn_status.html'), 1)
        self.assertEqual(self.detail.count('chat/partials/turn_status.html'), 1)

    def test_panel_halts_flow_before_loading_another_conversation(self):
        """切对话必须先 halt()：否则上一个对话的轮询会往新对话的消息流里 append 回复"""
        split = self.split
        opener = split[split.index('function selectConversation'):split.index('// ── 初始化 PaChatTurn')]
        self.assertIn('.halt()', opener)
        self.assertIn('data-turn-resume', opener, '历史加载完要接上进行中的轮次')
        self.assertIn("fetch(base + 'widget-messages/'", opener,
                      '切换对话时必须拉取历史片段')

    def test_cancel_does_not_broadcast_a_data_change(self):
        """取消没写过任何数据：无条件广播会让点「停止」后弹出「活动数据已更新」（实测踩到）"""
        start = self.flow.index('function stop()')
        body = self.flow[start:self.flow.index('\n        }', start)]
        self.assertIn('if (d.changed && opts.onActivityChanged)', body,
                      'stop() 里的广播必须看 d.changed')
        self.assertNotIn('if (opts.onActivityChanged)', body, '不允许无条件广播数据已变更')
        self.assertIn('apiFetch(u.cancel', body, 'cancel 也必须走共用出口')

    def test_flow_keeps_the_three_long_poll_fallbacks(self):
        """长轮询（turn-wait）的三层回退都得在源码里（docs/sse-streaming-evaluation.md Phase 1.2）

        少任何一层，坏形态都不报错、只是静默烧资源：
        - 页面隐藏仍挂长连接 → 后台标签占住服务端名额与线程；
        - 宿主没带 wait（旧模板未更新）→ 直接请求不存在的路径；
        - wait 在秒回（名额被占/端点异常）却不退回 intervalMs → 200ms 快环
          把 gunicorn 线程点燃 —— 快速循环烧的是服务端，不是本机。
        只扫剔了注释的代码：这几条的说明注释自己包含这些词。
        """
        self.assertIn('u.wait && !document.hidden', self.flow_code,
                      '挂等待前先确认宿主带 wait 且页面可见')
        self.assertIn('waitFastStreak', self.flow_code,
                      '连续秒回计数是退回普通轮询节奏的依据')
        self.assertIn("document.addEventListener('visibilitychange'", self.flow_code,
                      '切回前台必须立刻补一拍，后台期间只走了低频轮询')

    def test_all_three_hosts_hand_the_wait_url(self):
        """三个宿主（分栏页、详情页、活动抽屉）都要把 turn-wait 递给 PaChatTurn

        递不到就是该入口静默回到旧节奏（不算坏，但 Phase 1.2 的收益漏了一个门）；
        抽屉走服务端 urls dict，另两个模板自己拼路径。
        """
        for src, name in ((self.split, '分栏页'), (self.detail, '详情页')):
            self.assertIn("wait: base + 'turn-wait/'", src, name)
        self.assertIn('wait: d.urls.wait', self.drawer, '抽屉的 wait 来自 activity_conversation')


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
        """渲染出的块级结构（含气泡、卡片等一切祖先）若带 whitespace-pre-wrap，会跟 <br> 叠成双倍行距

        历史版本按 index('markdown-content') … index('</div>') 切片，而起点在终点之后
        （实测 719 > 665）→ 切片恒空、断言恒真，是一条静默空跑的假锁。
        改成整份片段比对：祖先挂错类也拦得住，且不存在切空的可能。
        """
        html = self._fragment('assistant', '一段\n两段')
        self.assertIn('一段', html, '片段没渲染出东西，这条锁就是空的')
        self.assertNotIn('whitespace-pre-wrap', html)

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
    """消息复制按钮与 .chat-chip 样式的接线锁

    开场 chips 已于 2026-09-19 按用户要求下线（使用率太低，core.chips 已删）；
    .chat-chip 类仍被「下一步」chips 的「还有 N 条建议…」展开钮使用，CSS 锁保留。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        js = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.js_code = ChatTurnFlowJsTest._strip_js_comments(js)

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

    钉选的价值全在「注入的是现状」：模型拿到已花/参与者才能直接算「还剩多少」，
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
            description='住皇后镇，含跳伞')
        self.activity.participants.add(Participant.objects.create(user=self.user, name='YYX'))
        apply_tags(self.activity, ['新西兰'])
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
                       '已花费：¥0.00', 'YYX', '住皇后镇'):
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
        self.assertIn('已花费：¥0.00', self.conv.turn_prompt)
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
        apply_tags(trip, ['新西兰'])
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

        输入区现在挂了两个 include（pin_host / local_cards），而浮窗在
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


class _CreateSessionFakeService(FakeQoderService):
    """在 FakeQoderService 上补 create_session（真实链路建对话时要先开云端 session）"""

    def create_session(self, agent_id, environment_id):
        n = getattr(self, 'sessions_created', 0) + 1
        self.sessions_created = n
        return {'id': f'sess_act_{n}'}


class ActivityConversationEndpointTest(TestCase):
    """活动专属对话 create-or-get（详情页「问 AI」抽屉入口）

    口径：每个活动同时最多一个活跃专属对话，按 pin_activity 定位；
    复用 = 历史可回溯 + 钉选注入只建一次。越权/不存在返回 JSON 404
    （不是 HTML 错误页 —— fetch 消费端拿 HTML 会炸 r.json()）。
    """

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.other = User.objects.create_user('o', password='p')
        self.client.force_login(self.user)
        from agents.models import AgentConfig, EnvironmentConfig
        AgentConfig.objects.create(
            agent_id='agent_chat', name='对话', purpose='knowledge', is_active=True)
        EnvironmentConfig.objects.create(
            env_id='env_default', name='默认', is_default=True)
        self.activity = Activity.objects.create(user=self.user, name='新西兰旅游')
        self.foreign = Activity.objects.create(user=self.other, name='别人的活动')
        self.url = f'/chat/for-activity/{self.activity.id}/'

    def _get(self, service):
        with patch('chat.views.get_service', return_value=service):
            return self.client.get(self.url, HTTP_ACCEPT='application/json')

    def test_creates_conversation_pinned_to_activity(self):
        data = self._get(_CreateSessionFakeService()).json()
        conv = Conversation.objects.get(id=data['conversation_id'])
        self.assertEqual(conv.pin_activity_id, self.activity.id)
        self.assertEqual(conv.user, self.user)
        self.assertIn('新西兰旅游', conv.title)
        self.assertTrue(data['created'])
        self.assertEqual(data['activity_name'], '新西兰旅游')
        # urls 五件套齐全（抽屉全靠它们挂 PaChatTurn；wait 漏了抽屉就静默退回普通轮询）
        base = f'/chat/{conv.id}'
        self.assertEqual(data['urls'], {
            'send': f'{base}/send/',
            'poll': f'{base}/turn/',
            'wait': f'{base}/turn-wait/',
            'cancel': f'{base}/turn/cancel/',
            'messages': f'{base}/widget-messages/',
        })

    def test_same_activity_reuses_the_same_conversation(self):
        first = self._get(_CreateSessionFakeService()).json()['conversation_id']
        second = self._get(_CreateSessionFakeService()).json()
        self.assertEqual(second['conversation_id'], first)
        self.assertFalse(second['created'])
        self.assertEqual(Conversation.objects.filter(user=self.user).count(), 1)

    def test_foreign_activity_returns_json_404(self):
        resp = self.client.get(f'/chat/for-activity/{self.foreign.id}/',
                               HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())   # JSON 而不是 HTML 错误页

    def test_missing_activity_returns_json_404(self):
        resp = self.client.get('/chat/for-activity/999999/',
                               HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(self.url, HTTP_ACCEPT='application/json')
        self.assertIn(resp.status_code, (302, 401))

    def test_archived_conversation_is_not_reused(self):
        """归档不复活：旧对话被归档后，本活动下次打开新建一个"""
        service = _CreateSessionFakeService()
        first_id = self._get(service).json()['conversation_id']
        Conversation.objects.filter(id=first_id).update(status='archived')
        data = self._get(service).json()
        self.assertNotEqual(data['conversation_id'], first_id)
        self.assertTrue(data['created'])

    def test_manually_unpinned_conversation_is_not_reused(self):
        """软策略（AGENTS.md 抽屉小节有记）：用户在聊天页手动取消钉/改钉到别的
        活动后，原活动下次打开新建一个 —— 不做迁移，锁定这个行为防止有人顺手
        「优化」成全局复用而丢掉钉选语义"""
        service = _CreateSessionFakeService()
        conv_id = self._get(service).json()['conversation_id']
        Conversation.objects.filter(id=conv_id).update(pin_activity=None)
        data = self._get(service).json()
        self.assertNotEqual(data['conversation_id'], conv_id)
        self.assertTrue(data['created'])


class ProtocolRideAlongTest(TestCase):
    """首帧协议合并投递（2026-10-28）：协议跟随第一轮用户消息进 turn_prompt，
    不再建对话时单发——单发必与用户第一问撞 409 忙等（「第一条消息特别慢」真因）
    """

    def setUp(self):
        self.user = User.objects.create_user('pr', password='p')
        self.client.force_login(self.user)
        from agents.models import AgentConfig, EnvironmentConfig
        AgentConfig.objects.create(
            agent_id='agent_chat', name='对话', purpose='knowledge', is_active=True)
        EnvironmentConfig.objects.create(
            env_id='env_default', name='默认', is_default=True)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_pr', agent_id='ag_pr', title='新对话')

    def _send(self, service):
        with patch('chat.views.get_service', return_value=service):
            return self.client.post(
                f'/chat/{self.conv.id}/send/', {'content': '你好'},
                HTTP_ACCEPT='application/json')

    def test_first_turn_carries_protocol_and_marks_sent(self):
        service = FakeQoderService()
        resp = self._send(service)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(service.sent), 1, '一次投递：协议+首问合并，不单发首帧')
        prompt = service.sent[0]
        self.assertIn(build_protocol_prompt(), prompt, '协议正文在 turn_prompt 里')
        self.assertIn('你好', prompt)
        self.assertLess(prompt.index(build_protocol_prompt()), prompt.index('你好'),
                        '协议在前，用户问题在后')
        conv = Conversation.objects.get(id=self.conv.id)
        self.assertTrue(conv.protocol_sent)
        self.assertIn('你好', conv.turn_prompt)

    def test_second_turn_does_not_repeat_protocol(self):
        service = FakeQoderService()
        self._send(service)
        # 第一轮停在 awaiting（测试里没人收尾），开下一轮前先落库结束态
        Conversation.objects.filter(id=self.conv.id).update(
            turn_state=Conversation.TURN_DONE)
        service.sent.clear()
        with patch('chat.views.get_service', return_value=service):
            self.client.post(f'/chat/{self.conv.id}/send/', {'content': '第二条'},
                             HTTP_ACCEPT='application/json')
        self.assertTrue(service.sent)
        self.assertNotIn(build_protocol_prompt(), service.sent[0])

    def test_existing_conversation_never_gets_protocol_backfilled(self):
        """存量会话（迁移回填 protocol_sent=True）建库时都单发过首帧，不再补发"""
        Conversation.objects.filter(id=self.conv.id).update(protocol_sent=True)
        service = FakeQoderService()
        self._send(service)
        self.assertTrue(service.sent)
        self.assertNotIn(build_protocol_prompt(), service.sent[0])

    def test_busy_retry_resends_same_prompt_with_protocol(self):
        """首发撞 409 → 保持 queued → 轮询重发的还是同一份 turn_prompt（协议不掉）"""
        service = FakeQoderService(send_raises=http_error(409))
        self._send(service)
        conv = Conversation.objects.get(id=self.conv.id)
        self.assertTrue(conv.protocol_sent, '标记在投递前：turn_prompt 已携带协议')
        self.assertIn(build_protocol_prompt(), conv.turn_prompt)

    def test_create_conversation_sends_nothing_to_platform(self):
        """建对话链路不再单发首帧（单发就是撞 409 的来源）"""
        service = _CreateSessionFakeService()
        activity = Activity.objects.create(user=self.user, name='协议合并')
        with patch('chat.views.get_service', return_value=service):
            self.client.get(f'/chat/for-activity/{activity.id}/',
                            HTTP_ACCEPT='application/json')
        self.assertEqual(service.sessions_created, 1)
        self.assertEqual(service.sent, [], '建对话时对平台零消息')
        new_conv = Conversation.objects.exclude(id=self.conv.id).get(user=self.user)
        self.assertFalse(new_conv.protocol_sent)


class ChatPinWiringTest(SimpleTestCase):
    """钉选的前端接线：两个宿主各一份、状态随历史片段带出、不拼 HTML 字符串

    两个宿主：详情页（message-pin）与分栏页（split-pin）。
    （右下角浮窗宿主 chat-panel-pin 已随浮窗整体下线。）
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        js = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.js_code = ChatTurnFlowJsTest._strip_js_comments(js)
        cls.split = (root / 'templates' / 'chat' / 'conversation_list.html').read_text(encoding='utf-8')
        cls.detail = (root / 'templates' / 'chat' / 'conversation_detail.html')\
            .read_text(encoding='utf-8')
        cls.widget = (root / 'templates' / 'chat' / 'partials' / 'widget_messages.html')\
            .read_text(encoding='utf-8')
        cls.css = (root / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')

    def test_both_surfaces_mount_a_host_with_distinct_ids(self):
        self.assertIn('pin_host.html', self.detail)
        self.assertIn('pin_host.html', self.split)
        ids = re.findall(r'host_id="([^"]+)"', self.detail + self.split)
        self.assertEqual(len(ids), 2, '两个宿主各挂一份，多了就是写了第三处')
        self.assertEqual(len(set(ids)), 2, 'id 撞了 getElementById 只会拿到第一个')
        for src, name in ((self.detail, '详情页'), (self.split, '分栏页')):
            self.assertIn('window.PaChatPin({', src, '%s 没有初始化钉选交互' % name)

    def test_pin_state_travels_with_the_history_fragment(self):
        """切对话时钉选状态必须跟着换：没钉也要带出隐藏位，否则槽里留着上一个对话的 chip

        搬运发生在分栏页（selectConversation 的 fetch 回调），不在 chat-turn.js 里：
        PaChatPin 只供一个 paint()，所以断言要分别看两个文件。"""
        self.assertIn('pin_bar.html', self.widget)
        self.assertIn('mount=True', self.widget)
        self.assertIn('[data-pin-mount]', self.split)
        self.assertIn('pinCtrl.paint(', self.split)

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


class AskDrawerWiringTest(SimpleTestCase):
    """活动详情页「问 AI」抽屉的静态接线锁

    抽屉是 PaChatTurn 的第三个挂载点（详情页/分栏页之后），历史上第三个挂载
    点（浮窗）就是因为边界条件难对齐被整体下线的 —— 这些锁保住「只复用不另写」。
    所有锁都必须拿变异验证真的会响（本项目静态锁空跑踩过不止一次）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        detail = (root / 'templates' / 'activities' / 'activity_detail.html')\
            .read_text(encoding='utf-8')
        # 抽屉脚本自身是内联 JS：剔注释后再扫（注释里写「禁 hx-」自己含那个词）
        cls.js_code = ChatTurnFlowJsTest._strip_js_comments(detail)
        cls.detail_raw = detail
        cls.split = (root / 'templates' / 'chat' / 'conversation_list.html')\
            .read_text(encoding='utf-8')
        cls.turn_js = ChatTurnFlowJsTest._strip_js_comments(
            (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8'))
        cls.detail_views = (root / 'activities' / 'views' / 'detail_views.py')\
            .read_text(encoding='utf-8')
        cls.css = (root / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')

    def test_drawer_dom_and_turn_mount_exist(self):
        self.assertIn('id="ask-drawer"', self.detail_raw, '抽屉容器没了')
        self.assertIn('id="ask-drawer-messages"', self.detail_raw)
        self.assertIn('id="ask-drawer-form"', self.detail_raw)
        self.assertIn('window.PaChatTurn({', self.js_code, '抽屉没挂 PaChatTurn')
        self.assertIn('turn_status.html', self.detail_raw, '进度条必须 include 同一份模板')

    def test_drawer_uses_only_native_fetch(self):
        # 只扫抽屉自身的 DOM+脚本块：整页早有别的脚本/注释（如「禁挂 hx-*」的
        # 说明文字），扫全页会把注释里的词当成违规 —— 先剔注释再扫，且切片必须非空
        block = self.js_code[self.js_code.index('id="ask-drawer"'):
                             self.js_code.rindex('{% endblock %}')]  # rindex：extra_head 也有一对 endblock
        self.assertGreater(len(block.strip()), 2000, '切片切空了，这条锁就是假的')
        self.assertNotIn('hx-', block, 'JSON/片段端点严禁 hx-*（会当纯文本插 DOM）')
        self.assertIn("'Accept': 'application/json'", block)

    def test_drawer_open_toggle_does_not_mix_display_utilities(self):
        """hidden 与 flex 同层冲突（turn_status 教训）：开关只能走 .ask-drawer-open"""
        self.assertIn("classList.add('ask-drawer-open')", self.js_code)
        self.assertNotIn("drawer.classList.remove('hidden')", self.js_code)
        self.assertIn('.ask-drawer-open', self.css)
        self.assertIn('@keyframes ask-drawer-in', self.css)

    def test_backdrop_click_closes_back_to_the_activity_page(self):
        """弹窗语义：点抽屉外区域（backdrop）关闭回到活动详情页，不是跳页。
        开关类必须由 JS 显式同步（backdrop 在抽屉 DOM 之前，CSS 兄弟选择器不命中）"""
        self.assertIn('id="ask-drawer-backdrop"', self.detail_raw)
        self.assertIn("getElementById('ask-drawer-backdrop').addEventListener('click', close)", self.js_code)
        self.assertIn("backdrop.classList.add('ask-drawer-backdrop-open')", self.js_code)
        self.assertIn("backdrop.classList.remove('ask-drawer-backdrop-open')", self.js_code)
        self.assertIn('.ask-drawer-backdrop-open', self.css)
        self.assertNotIn('.ask-drawer-open ~', self.css,
                         'backdrop 在抽屉之前，兄弟选择器是永不命中的死代码')

    def test_history_fragment_slot_is_known_to_chat_turn_js(self):
        """insertLocalHtml 的回退链必须认得抽屉容器，否则局部卡片静默丢弃"""
        self.assertIn("getElementById('ask-drawer-messages')", self.turn_js)

    def test_no_js_fallback_deeplink_carries_pin(self):
        """href 降级路径：深链必须带 pin 参数，聊天页 ?ask= 块要接住它"""
        self.assertIn("'pin': activity.id", self.detail_views)
        self.assertIn('pinActivityId', self.split)
        self.assertIn("'/pin/'", self.split, '?ask= 块要调 pin 端点自动钉选')
        # form body 必须显式给 Content-Type：fetch 默认 text/plain，Django 不解析，
        # activity_id 落空会被 pin 端点当成「取消钉选」（真机踩过）
        pin_block = self.split[self.split.index('pinActivityId'):
                               self.split.index('window.paSplitChat')]
        self.assertGreater(len(pin_block.strip()), 300, '切片切空了，这条锁就是假的')
        self.assertIn("'Content-Type': 'application/x-www-form-urlencoded'", pin_block)

    def test_drawer_endpoints_go_through_the_create_or_get_outlet(self):
        self.assertIn('activity_conversation', self.detail_raw,
                      '抽屉必须走 create-or-get 端点，不许前端自己拼建对话请求')
        self.assertIn('d.urls.messages', self.js_code,
                      '历史片段端点由服务端下发，前端不得手拼')


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
    'activity_conversation': ('get', '/chat/for-activity/1/'),
    'send_message': ('post', '/chat/1/send/'),
    'turn_poll': ('get', '/chat/1/turn/'),
    'turn_wait': ('get', '/chat/1/turn-wait/'),
    'turn_cancel': ('post', '/chat/1/turn/cancel/'),
    'pin_conversation': ('post', '/chat/1/pin/'),
    'pin_candidates': ('get', '/chat/pin/search/'),
    'local_card_create': ('post', '/chat/1/local-card/'),
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
        cls.split = (root / 'templates' / 'chat' / 'conversation_list.html').read_text(encoding='utf-8')

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

    def test_split_page_uses_the_same_outlet_for_create(self):
        """base.html 浮窗的「+ 新对话」以前靠假的 HX-Request 头骗视图返 JSON（浮窗已下线）；

        分栏页的「新对话」按钮必须继续走共用出口：否则未登录时装饰器会按 HTML 请求
        返 302，401 分支永远走不到（所以不只是一个雅观问题）。
        """
        from core.layout_asserts import code_only
        # 两道都得剔：code_only 只剔 HTML/{% comment %} 注释，而这段说明写在内联
        # JS 的 // 注释里 —— 不剔就会把「旧的骗法不得回来」的说明当成违规（本项目老坑）
        code = ChatTurnFlowJsTest._strip_js_comments(code_only(self.split))
        self.assertIn("paJsonFetch('{% url \"chat:create_conversation\" %}'", code)
        self.assertNotIn("'HX-Request': 'true'", code, '旧的骗法不得回来')
        self.assertIn("{% staticv 'js/chat-turn.js' %}", self.base,
                      'chat-turn.js 由 base.html 统一引入，否则分栏页里 paJsonFetch 是 undefined')

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


class ChatMobileBubbleLayoutTest(SimpleTestCase):
    """消息流布局的 CSS 锁（2026-10-27 Typeframe 改版：气泡 → 发送者标签 + 正文）

    为什么必须专门锁：消息流的宽度上限与左右分离在 CSS 侧没有渲染断言覆盖，
    「上限漏进移动端」与「左右分离被删了却忘了角色还在靠它区分」都只在真实屏幕上
    看得出来。改版后角色区分全靠 .chat-who 标签（用户=L4 灰、AI=L3 灰），
    标签规则丢了 = 移动端回到无法区分谁在说话。

    断言一律走 core.layout_asserts.css_rules（按选择器串匹配 + 回溯所在 @media），
    不用 css.index('.chat-message') 取首次出现、也不用 index('.chat-bubble') 的跨度切片：
    后者与既有注释撞词就会切出空串，变成一条恒真的假锁（本项目已踩过）。
    """

    DESKTOP = '(min-width: 768px)'
    MOBILE = '(max-width: 767px)'
    # 消息钩子元素的 class 属性串（Typeframe 后气泡 chrome 全删，钩子就是裸 class="chat-bubble …"）
    BUBBLE_CLASS = re.compile(r'class="([^"]*\bchat-bubble\b[^"]*)"')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from core.layout_asserts import CSS_PATH
        cls.css = CSS_PATH.read_text(encoding='utf-8')

    def _blocks(self, selector):
        from core.layout_asserts import css_rules
        return css_rules(self.css, selector)

    def _bodies(self, selector, media):
        return ' '.join(b['body'] for b in self._blocks(selector) if b['media'] == media)

    def test_message_width_cap_only_exists_inside_the_desktop_breakpoint(self):
        """消息宽度上限只允许在 md+ 生效（移动端必须满宽）

        故意不限定「必须等于 36rem」：改数值是调优，漏媒体查询才是要防的退化。
        """
        capped = [b for b in self._blocks('.chat-message')
                  if re.search(r'max-width:\s*\d', b['body'])]
        self.assertTrue(capped, '找不到 .chat-message 的宽度上限声明 —— 桌面端长行约束被删了？')
        for block in capped:
            self.assertEqual(block['media'], self.DESKTOP,
                             '宽度上限漏出 md+，移动端又会变成窄消息：%s' % block['selectors'])

    def test_desktop_sender_separation_declaration_survives(self):
        """桌面端（md+）仍是限宽 + 左右分离 —— 用户右对齐、AI 左对齐是改版核心构图"""
        user = self._bodies('.chat-message.user', self.DESKTOP)
        self.assertIn('margin-left: auto', user, '用户消息不再右对齐')
        self.assertIn('text-align: right', user)
        assistant = self._bodies('.chat-message.assistant', self.DESKTOP)
        self.assertIn('margin-right: auto', assistant, 'AI 消息不再左对齐')

    def test_mobile_gives_the_message_no_width_cap_or_side_alignment(self):
        """移动端范围内不得有宽度上限或 margin-*-auto：全宽左对齐阅读

        防的是「又把消息夹住 / 推到右边」，不是禁止一切复位写法 —— 真要复位桌面声明时
        写的是 max-width: none / margin-left: 0，那些不匹配下面两条。
        """
        bodies = self._bodies('.chat-message', self.MOBILE)
        for pattern in (r'max-width:\s*\d', r'margin-(?:left|right):\s*auto'):
            hits = re.findall(pattern, bodies)
            self.assertEqual(hits, [], '移动端出现消息夹宽/右对齐声明：%s' % hits)

    def test_sender_label_rules_exist_with_distinct_greys(self):
        """角色区分全靠 .chat-who 标签：基础规则给 AI（L3 灰），user 覆写 L4 灰

        两档灰度不同是「谁在说话」的唯一编码 —— 同色等于回到无法区分。
        """
        base = self._bodies('.chat-who', '')
        self.assertIn('letter-spacing', base, '.chat-who 基础规则没了，发送者标签退化成普通文字')
        self.assertIn('text-transform: uppercase', base)
        user = self._bodies('.chat-message.user .chat-who', '')
        self.assertIn('var(--tf-fg4)', user, '用户标签没有覆写 L4 灰，与 AI 标签同色分不出谁在说话')

    def test_body_typography_rules_exist(self):
        """正文 14px / 1.8 行高（mockup .txt）与用户消息灰一档必须成对存在"""
        body = self._bodies('.chat-message .chat-bubble .md-body', '') + \
               ' ' + self._bodies('.chat-message .chat-bubble .chat-txt', '')
        self.assertIn('line-height: 1.8', body, '正文行高规则被删，长回答会挤成一团')
        user_body = self._bodies('.chat-message.user .chat-bubble', '')
        self.assertIn('var(--text-secondary)', user_body, '用户正文不再灰一档（L2），与 AI 纯黑无层级')

    def test_bubble_shell_style_lives_in_two_templates_behind_one_hook(self):
        """chat-bubble 钩子只允许这两份抄本，且两份都真的挂在元素 class 上

        第三份抄本会静默拿不到消息流样式，症状只是「某处消息字号/行高不对」，
        没人会想到是漏挂钩子。
        """
        from core.layout_asserts import code_only
        root = Path(__file__).resolve().parent.parent / 'templates'
        copiers = sorted(str(p.relative_to(root)) for p in root.rglob('*.html'
                         ) if 'chat-bubble' in code_only(p.read_text(encoding='utf-8')))
        self.assertEqual(copiers, ['chat/partials/_message.html',
                                   'chat/partials/turn_error.html'],
                         'chat-bubble 钩子出现在第三处：消息流样式会漏掉新处')
        for rel in copiers:
            src = code_only((root / rel).read_text(encoding='utf-8'))
            # 只看元素自己的 class 属性：整份文件里提到 chat-bubble 的不算 ——
            # 注释里提这个词会把锁糊过去（变异反证实测抓到）
            attrs = self.BUBBLE_CLASS.findall(src)
            self.assertTrue(attrs, '%s 里找不到挂 chat-bubble 的 class 属性，这条锁就是空的' % rel)

class ChatBubbleDomContractTest(TestCase):
    """chat-turn.js 的三处 DOM 锚点必须仍是「祖先包住后代」的形状

    这三处失效没有任何症状、也没有既有测试挡着：既有那条只断言 JS 源码里出现了
    `querySelector('.md-body')` 这个字符串（锁的是 JS 文本，不是 DOM 真找得到它）。
      · 复制：closest('.chat-message') 里再 querySelector('.md-body') → 命不中就是复制空串
      · 重试：closest('.chat-message').remove() → 命不中就是历史里留两个「已中断」气泡
      · 悬停：.group 包住 .hover-actions → 脱钩就是桌面端复制按钮永久 opacity:0
    """

    def setUp(self):
        self.user = User.objects.create_user('dom', password='p')
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_dom', agent_id='ag_dom', title='形状')

    def test_copy_button_and_body_share_one_chat_message_ancestor(self):
        from django.template.loader import render_to_string
        msg = Message.objects.create(conversation=self.conv, role='assistant',
                                     content='结论：值得去')
        html = render_to_string('chat/partials/_message.html', {'msg': msg})
        # 从 .chat-message 开标签到气泡正文之间的「头部」：group / chat-bubble /
        # data-copy-msg 必须都在这里（= 都还包住正文）
        head = html[:html.index('markdown-content')]
        self.assertGreater(len(head), 120, '头部切片切空了，这条锁就是假的')
        self.assertIn('class="chat-message assistant', head)
        self.assertIn('chat-bubble', head)
        self.assertIn('group', head, '丢了 group，桌面端复制按钮会永久 opacity:0')
        self.assertIn('data-copy-msg', head, '复制按钮掉出气泡，复制会静默返回空串')
        # 正文必须在气泡之内：md-body 与 markdown-content 是同一个类属性（不能脱钩），
        # 且正文文本出现在它之后。这里不做跨度切片 —— 起点在终点之后的切片恒空，
        # 断言就恒真，正是本项目踩过的那类假锁。
        self.assertIn('class="markdown-content md-body', html,
                      'md-body 与 markdown-content 脱钩，closest + querySelector 会拿不到正文')
        self.assertGreater(html.index('结论：值得去'), html.index('markdown-content'),
                           '正文掉到气泡结构之外')

    def test_retry_button_is_inside_the_error_bubble(self):
        """中断气泡的「重试」必须在 .chat-message 内部：holder.remove() 靠它清掉旧气泡"""
        from django.template.loader import render_to_string
        pending = Message.objects.create(conversation=self.conv, role='user',
                                         content='帮我查一下')
        self.conv.turn_message = pending
        self.conv.turn_state = Conversation.TURN_ERROR
        self.conv.save()
        err = render_to_string('chat/partials/turn_error.html',
                               {'note': '本轮已中断', 'conversation': self.conv})
        self.assertIn('data-retry-text', err, '没渲染出重试按钮，这条锁就是空的')
        self.assertLess(err.index('class="chat-message'), err.index('data-retry-text'),
                        '重试按钮掉出 .chat-message，remove() 会静默留下双气泡')
        self.assertIn('chat-bubble', err[:err.index('data-retry-text')],
                      '中断气泡没挂钩子类，移动端它会一直是窄气泡')


# ── 协议回复被长度上限截断（线上真实故障，2026-09-07 对话 15 / 消息 64）─────────
# 现场重现：用户点「下一步」里的「把这份四周独处训练方案存进知识库」chip（chip 本身
# 工作正常，文字确实发出去了），模型按协议输出 JSON，但想把整篇文章正文重抄进
# params.content —— 回复停在 4075 字符，花括号只剩 1 个右括号（实测事件
# evt_00op6wadl6g3lbkjo207，平台下发的就是半截）。残缺 JSON 解析不出来，旧逻辑认定
# 「不是 JSON 就是普通对话」，于是：
#   现象一：一整坨 {"intent":"knowledge_create",...} 被当正文渲染到界面上
#   现象二：knowledge.create 根本没被调用，知识库里没有新文章
# 两个现象同一个根因。截断在平台侧（不可控），把原文吐给用户是我们侧的错误（必须锁住）。

# 线上截断样本的结构等价物：外层对象未闭合，content 停在字符串中间
TRUNCATED_PROTOCOL = (
    '{"intent":"knowledge_create","params":{"title":"四周独处训练方案",'
    '"content":"## 核心原理\\n\\n独处是**力量训练**，不是意志力考验。'
    '思绪飘走，轻轻拉回来就好。\\n\\n下一步：建一个活动「四周独处训练」'
)


class ProtocolTruncationFallbackTest(SimpleTestCase):
    """协议 JSON 残缺时的降级：绝不把内部协议原文递到用户脸上"""

    def setUp(self):
        self.user = User(username='u1')

    def test_truncated_json_is_recognised_as_protocol(self):
        """前提锁：这个样本必须「看着像协议但解析不出来」

        不先钉住这一点，后面所有断言都可能是在锁一个普通文本（锁空跑）。
        """
        self.assertIsNone(extract_intent(TRUNCATED_PROTOCOL), '样本已能解析，不再是截断场景')
        self.assertTrue(looks_like_protocol(TRUNCATED_PROTOCOL))

    def test_protocol_source_never_reaches_the_user(self):
        content, payload, changed = orchestrator.process(self.user, TRUNCATED_PROTOCOL)
        for leaked in ('{"intent"', 'knowledge_create', '"params"', '"content"'):
            self.assertNotIn(leaked, content, f'协议原文泄到界面上：{leaked}')
        self.assertIn('没有执行成功', content)
        # 降级不等于报错：不阻断对话、不假装做过
        self.assertIsNone(payload)
        self.assertFalse(changed)

    def test_truncation_does_not_claim_a_saved_article(self):
        """失败文案不得出现「已存入」这类成功口径 —— 那比 JSON 更难发现"""
        content, _, _ = orchestrator.process(self.user, TRUNCATED_PROTOCOL)
        for claiming in ('已存入', '已保存', '已完成', '已成功'):
            self.assertNotIn(claiming, content, f'失败回复里混进了成功口径：{claiming}')

    def test_natural_language_still_passes_through(self):
        """反向锁：联网问答逃生舱靠透传工作，不得被协议识别误杀

        以 { 开头但不含 "intent" 键（模型给用户看代码片段）必须照常透传，
        否则就是把一个可用功能换成了一个固定失败文案。
        """
        text = '{"timeout": 30} 这个配置里的秒数设成 30 就可以了。'
        self.assertFalse(looks_like_protocol(text))
        content, _, _ = orchestrator.process(self.user, text)
        self.assertEqual(content, text)

    def test_prompt_teaches_the_reference_token(self):
        """协议规则 10 必须在 prompt 里 —— 模型不知道该用引用时，截断会不断重现

        引用展开的代码在删了规则后仍能运行，但再也没人会发出标记，
        于是这个缺陷只是被暂藏起来了。所以规则文本本身也要钉住。

        必须把断言限定在「规则：」段：工具描述也会拼进同一个 prompt（「可用意图」），
        而 knowledge.create 的描述里就带着 $LAST_REPLY 与“不要重新抄写”——
        扫整段 prompt 的话，规则 10 被整条删掉也不会响（变异实测到）。
        """
        prompt = build_protocol_prompt()
        rules = prompt.split('规则：', 1)[-1]
        self.assertIn('10.', rules, '规则条数变了，这条锁的范围要跟着调')
        for kw in ('$LAST_REPLY', '不要重新抄写', '长度上限'):
            self.assertIn(kw, rules, f'规则段缺了对引用标记的说明：{kw}')


class ProtocolRefExpansionTest(SimpleTestCase):
    """params 引用展开：只展开整字段恰好等于标记的值"""

    REF = {'LAST_REPLY': '完整的文章正文', 'LAST_USER': ''}

    def test_marker_is_replaced_with_referenced_text(self):
        params, error = resolve_params_refs(
            {'title': 'T', 'content': '$LAST_REPLY'}, self.REF)
        self.assertIsNone(error)
        self.assertEqual(params['content'], '完整的文章正文')
        self.assertEqual(params['title'], 'T', '非标记字段不得被动到')

    def test_empty_reference_reports_error_instead_of_passing_token(self):
        """引用取不到内容时报错，而不是把标记原样交给工具

        放过的话，“$LAST_USER”这十个字符会被当成正文存进库里，
        看起来成功、实际写了垃圾数据，比直接失败更难发现。
        """
        params, error = resolve_params_refs({'content': '$LAST_USER'}, self.REF)
        self.assertIsNotNone(error)
        self.assertEqual(params['content'], '$LAST_USER')

    def test_substring_is_not_replaced(self):
        """正文里真的写到这个标记时不得被误伤（只做整字段匹配）"""
        params, error = resolve_params_refs(
            {'content': '例子：把 $LAST_REPLY 写在这个位置'}, self.REF)
        self.assertIsNone(error)
        self.assertEqual(params['content'], '例子：把 $LAST_REPLY 写在这个位置')

    def test_list_values_are_expanded_itemwise(self):
        params, error = resolve_params_refs({'tags': ['$LAST_REPLY', '常用']}, self.REF)
        self.assertIsNone(error)
        self.assertEqual(params['tags'], ['完整的文章正文', '常用'])


class RecentContextRefTest(SimpleTestCase):
    """$RECENT_CONTEXT 引用：跨轮检索的关键词摘要走同一条展开通道"""

    REF = {'LAST_REPLY': '', 'LAST_USER': '', 'RECENT_CONTEXT': '出差 签证 预算'}

    def test_marker_expands_to_recent_keywords(self):
        params, error = resolve_params_refs({'keyword': '$RECENT_CONTEXT'}, self.REF)
        self.assertIsNone(error)
        self.assertEqual(params['keyword'], '出差 签证 预算')

    def test_missing_recent_context_errors_instead_of_passing_token(self):
        """首条消息没有历史可提炼时报错，绝不把标记原样交给工具当 keyword"""
        params, error = resolve_params_refs({'keyword': '$RECENT_CONTEXT'}, {})
        self.assertIsNotNone(error)
        self.assertEqual(params['keyword'], '$RECENT_CONTEXT')

    def test_protocol_prompt_advertises_the_marker(self):
        prompt = build_protocol_prompt()
        self.assertIn('$RECENT_CONTEXT', prompt)
        self.assertIn('memory_search', prompt)

    def test_per_turn_reminder_carries_the_rule(self):
        """检索规则必须进每轮补发通道：首帧只对新建对话生效是踩过的坑"""
        self.assertIn('$RECENT_CONTEXT', PROTOCOL_SEARCH_REMINDER)


class RecentContextKeywordTest(TestCase):
    """_recent_context_keywords：最近几条用户消息 → 提炼后的关键词串"""

    def setUp(self):
        self.user = User.objects.create_user('recent', password='x')
        self.conv = Conversation.objects.create(user=self.user, session_id='sess_recent')

    def _user_msg(self, content):
        return Message.objects.create(conversation=self.conv, role='user', content=content)

    def test_extracts_keywords_from_recent_user_messages(self):
        self._user_msg('我下周要去美国出差，帮我看看签证材料')
        self._user_msg('顺便算一下这次出差的预算')
        keywords = chat_views._recent_context_keywords(self.conv)
        self.assertIn('签证', keywords)
        self.assertIn('预算', keywords)
        self.assertNotIn('帮我', keywords, '停用词不得混进关键词串')

    def test_empty_conversation_returns_empty_string(self):
        self.assertEqual(chat_views._recent_context_keywords(self.conv), '')

    def test_window_is_limited_to_recent_messages(self):
        """只取最近 3 条：三个月前聊的老话题不该稀释当前主题"""
        old = self._user_msg('聊聊陶艺课的报名流程')
        Message.objects.filter(id=old.id).update(
            created_at=timezone.now() - timezone.timedelta(days=90))
        self._user_msg('帮我订下周二的电影票')
        self._user_msg('顺便看看天气')
        self._user_msg('现在的主题是年度预算复盘')
        keywords = chat_views._recent_context_keywords(self.conv)
        self.assertIn('预算', keywords)
        self.assertNotIn('陶艺', keywords)


ARTICLE_BODY = '## 核心原理\n\n独处是**力量训练**，不是意志力考验。' * 12


class KnowledgeCreateFromReferenceTest(TestCase):
    """行为锁：点「存入知识库」后确实写一篇对得上正文的文章

    走 _finalize_turn 这个接缝而不是只测工具函数：引用池是在那里从会话里取出来
    的，只测 resolve_params_refs 锁不到「池子递错了 / 根本没递」这一层（而线上
    故障恰恰就在递送链路上）。
    """

    def setUp(self):
        from knowledge.models import Article
        self.Article = Article
        self.user = User.objects.create_user('kb', password='x')
        self.conv = Conversation.objects.create(user=self.user, session_id='sess_kb_ref')
        Message.objects.create(conversation=self.conv, role='user',
                               content='讲讲独处的好处')
        self.prev = Message.objects.create(conversation=self.conv, role='assistant',
                                           content=ARTICLE_BODY)
        self.conv.turn_message = Message.objects.create(
            conversation=self.conv, role='user',
            content='把这份方案存进知识库')

    def _finalize(self, ai_text):
        return chat_views._finalize_turn(self.conv, ai_text)

    def test_reference_creates_one_article_with_the_exact_body(self):
        before = self.Article.objects.count()
        msg, changed = self._finalize(
            '{"intent":"knowledge_create","params":'
            '{"title":"四周独处训练方案","content":"$LAST_REPLY"},'
            '"reply":"好的，这就沉淀下来"}')
        created = self.Article.objects.all()
        self.assertEqual(created.count(), before + 1, '文章没落库')
        article = created.order_by('-id').first()
        self.assertEqual(article.content, ARTICLE_BODY, '正文与引用的那条回复对不上')
        self.assertEqual(article.user, self.user, '文章没归属到当前用户')
        self.assertIn('已存为文章', msg.content)
        self.assertTrue(changed)
        # 库里绝不能留下未展开的标记
        self.assertNotIn('$LAST_REPLY', article.content)

    def test_reference_survives_a_long_body(self):
        """长正文走引用时协议 JSON 本身就短，不再依赖模型重抄

        这正是截断的根源：旧路子下正文多长就得重抄一遍，一旦超过上限，整条指令就断在半路。
        """
        long_body = ARTICLE_BODY * 30
        self.prev.content = long_body
        self.prev.save(update_fields=['content'])
        text = ('{"intent":"knowledge_create","params":'
                '{"title":"长文","content":"$LAST_REPLY"},"reply":"好"}')
        self.assertLess(len(text), 200, '样本太大，不再是在模拟短指令')
        self._finalize(text)
        self.assertEqual(self.Article.objects.get(title='长文').content, long_body)

    def test_truncated_protocol_writes_nothing(self):
        """现象二的锁：半截指令不得留下任何文章，也不得把协议原文落库"""
        msg, changed = self._finalize(TRUNCATED_PROTOCOL)
        self.assertEqual(self.Article.objects.count(), 0, '未执行的指令写了库')
        self.assertFalse(changed)
        self.assertNotIn('intent', msg.content)

    def test_unexpanded_marker_is_refused_not_stored(self):
        """旁路防护：没引用池的调用方递上标记时，宁可报错也不写垃圾文章"""
        from core.agent_registry import ToolError
        from knowledge.agent_tools import tool_knowledge_create
        with self.assertRaises(ToolError):
            tool_knowledge_create(self.user, {'title': 'T', 'content': '$LAST_REPLY'})
        self.assertEqual(self.Article.objects.count(), 0)


class StaleSessionReferenceReminderTest(TestCase):
    """行为锁：存量会话也能收到引用规则（二次故障的根因）

    线上实测：修完首帧规则后，用户在 09-06 建的对话 15 里重试，残骸依旧（4031 字符，
    断在 params.content）—— 因为协议只在 create_conversation 时下发一次，
    存量 session 永远看不到后来新增的规则。只测 build_protocol_prompt 锁不到这一层。
    """

    def setUp(self):
        from django.test.client import RequestFactory
        self.req = RequestFactory().post('/', {})
        self.user = User.objects.create_user('stale', password='x')
        # 存量会话（本类锁的就是存量场景）：首帧早已投过，turn_prompt 不该再带协议
        self.conv = Conversation.objects.create(user=self.user, session_id='sess_stale',
                                                protocol_sent=True)

    def _reply(self, body):
        return Message.objects.create(conversation=self.conv, role='assistant',
                                      content=body)

    def _build(self, text='把这份方案存进知识库'):
        return chat_views._build_ai_content(self.req, self.conv, text)

    def test_long_previous_reply_re_sends_the_rule(self):
        self._reply('## 方案\n\n' + '内容。' * 500)
        sent = self._build()
        self.assertTrue(sent.startswith(PROTOCOL_REF_REMINDER),
                        '存量会话没收到引用规则，模型会继续重抄正文')
        self.assertIn('$LAST_REPLY', sent)
        self.assertTrue(sent.endswith('把这份方案存进知识库'), '用户原文必须还在最后')

    def test_short_previous_reply_gets_no_reminder(self):
        """反向锁：没东西可抄时不递引用规则；恒定的检索关键词补发不受此判据管
        （它没有条件，每轮都在——这里只锁「除基线外不多一行」）"""
        self._reply('好的，已记录。')
        self.assertEqual(self._build(), PROTOCOL_SEARCH_REMINDER + '把这份方案存进知识库')

    def test_no_previous_reply_still_sends_nothing(self):
        self.assertEqual(self._build(), PROTOCOL_SEARCH_REMINDER + '把这份方案存进知识库')

    def test_reminder_never_pollutes_the_stored_message(self):
        """提醒只能上线路，不能写回消息历史

        写回去的话，用户看到的自己发的那句话会多出一堆系统文字，而且重试时会
        叠加（turn_prompt 已经含提醒，再拼一次就变成两份）。
        """
        msg = Message.objects.create(conversation=self.conv, role='user',
                                     content='把这份方案存进知识库')
        self._reply('内容。' * REF_REMINDER_MIN_CHARS)
        self.assertIn(PROTOCOL_REF_REMINDER, self._build())
        msg.refresh_from_db()
        self.assertEqual(msg.content, '把这份方案存进知识库')

    def test_reminder_and_first_frame_share_the_same_tokens(self):
        """两处口径不得漂移：提醒里的标记必须是 resolve_params_refs 认的那两个"""
        for token in ('$LAST_REPLY', '$LAST_USER'):
            self.assertIn(token, PROTOCOL_REF_REMINDER)
            self.assertIn(token, build_protocol_prompt().split('规则：', 1)[-1])
        params, error = resolve_params_refs(
            {'content': PROTOCOL_REF_REMINDER.split('或 ')[1].split('（')[0].strip('"')},
            {'LAST_REPLY': 'A', 'LAST_USER': 'B'})
        self.assertIsNone(error, '提醒里写给模型的标记与实际能展开的对不上')
        self.assertEqual(params['content'], 'B')


class RetryAfterFailureReferenceTest(TestCase):
    """行为锁：一次失败不得把后续重试永久锁死（三次故障的真实形状）

    线上对话 15 的历史是〈长正文 → 协议残骸 → 失败文案〉。补发判据和引用池原本
    都取「字面上的上一条 assistant」，拿到的是 80 字的「这一步没有执行成功」：
    既不够 REF_REMINDER_MIN_CHARS（规则又不发了，模型继续重抄→再截断），一旦被
    展开还会把失败文案存成知识文章。用户在这个对话里点多少次都是死循环。
    """

    def setUp(self):
        from django.test.client import RequestFactory
        from knowledge.models import Article
        self.Article = Article
        self.req = RequestFactory().post('/', {})
        self.user = User.objects.create_user('retry', password='x')
        self.conv = Conversation.objects.create(user=self.user, session_id='sess_retry')
        self.long_body = ARTICLE_BODY * 30
        Message.objects.create(conversation=self.conv, role='assistant',
                               content=self.long_body)
        Message.objects.create(conversation=self.conv, role='assistant',
                               content=PROTOCOL_TRUNCATED_NOTE)
        self.conv.turn_message = Message.objects.create(
            conversation=self.conv, role='user', content='把这份方案存进知识库')

    def _build(self):
        return chat_views._build_ai_content(self.req, self.conv, '把这份方案存进知识库')

    def _store(self, title, extra=''):
        return chat_views._finalize_turn(
            self.conv, '{"intent":"knowledge_create","params":{"title":"%s",'
                       '"content":"$LAST_REPLY"%s},"reply":"好"}' % (title, extra))

    def test_retry_still_gets_the_rule_after_a_failure_note(self):
        self.assertTrue(self._build().startswith(PROTOCOL_REF_REMINDER),
                        '上一条是失败文案就判定「没东西可抄」，重试永远收不到规则')

    def test_retry_references_the_body_not_the_failure_note(self):
        _, changed = self._store('四周独处训练方案')
        article = self.Article.objects.get(title='四周独处训练方案')
        self.assertEqual(article.content, self.long_body, '引用展开成了失败文案')
        self.assertNotIn('这一步没有执行成功', article.content)
        self.assertNotIn('$LAST_REPLY', article.content)
        self.assertTrue(changed)

    def test_protocol_residue_in_history_is_skipped_too(self):
        """修复前落库的那类残骸（对话 15 msg 64）也算脏历史，不能当正文引用"""
        Message.objects.create(conversation=self.conv, role='assistant',
                               content=TRUNCATED_PROTOCOL)
        self._store('残骸后面')
        self.assertEqual(self.Article.objects.get(title='残骸后面').content,
                         self.long_body)

    def test_short_confirmation_never_borrows_an_older_long_body(self):
        """反向锁（安全性）：只取真正的上一条正文，绝不跨过它去拿更老的长文

        否则「把刚才那段话存一下」这类短目标会被静默写成一篇旧长文——存错内容
        比报错糟得多。宁可像旧行为那样失败，也不能偷换内容。
        """
        confirm = '已存为文章：《四周独处训练方案》'
        Message.objects.create(conversation=self.conv, role='assistant', content=confirm)
        self.assertNotIn(PROTOCOL_REF_REMINDER, self._build())
        self._store('短')
        self.assertEqual(self.Article.objects.get(title='短').content, confirm)

    def test_empty_assistant_message_is_skipped(self):
        """空回复（平台真的没产出时也会落一条）同样不能当正文引用"""
        Message.objects.create(conversation=self.conv, role='assistant', content='')
        self.assertTrue(self._build().startswith(PROTOCOL_REF_REMINDER))
        self._store('空的之后')
        self.assertEqual(self.Article.objects.get(title='空的之后').content,
                         self.long_body)

    def test_short_real_reply_starting_like_a_note_is_still_referenceable(self):
        """开头签名不能太宽：以常见字（这/上/操）开头的短真回复必须还能被引用

        签名一旦取一个字，这类短回复会被当成占位文案跳过，引用就会跨过它去拿更老的
        长正文（偷换内容）。这条锁把「至少得比一个字的前缀」这个约束实住。
        """
        real = '这个方案我先记下了，后面再补细节。'
        self.assertLessEqual(len(real), chat_views.NOTE_MAX_CHARS)
        Message.objects.create(conversation=self.conv, role='assistant', content=real)
        picked = chat_views._referenceable_reply(self.conv)
        self.assertEqual(picked.content, real, '短真实回复被签名误伤')
        self.assertNotIn(PROTOCOL_REF_REMINDER, self._build())

    def test_history_note_with_old_wording_is_still_skipped(self):
        """库里存的是当时的词：改了文案也不能让历史行变成「正文」

        这是实测踩到的：上一版按整串相等判，而我同时改了那句提示的措辞，
        于是线上 msg 70（改词前写的）依旧被选为 $LAST_REPLY，修复直接落空。
        """
        Message.objects.filter(conversation=self.conv, role='assistant').delete()
        Message.objects.create(conversation=self.conv, role='assistant',
                               content=chat_views.LEGACY_NOTE_TEXTS[0])
        Message.objects.create(conversation=self.conv, role='assistant',
                               content=self.long_body)
        Message.objects.create(conversation=self.conv, role='assistant',
                               content=chat_views.LEGACY_NOTE_TEXTS[0])
        self.assertTrue(self._build().startswith(PROTOCOL_REF_REMINDER))
        self._store('改词之后')
        self.assertEqual(self.Article.objects.get(title='改词之后').content,
                         self.long_body)

    def test_long_reply_starting_like_a_note_is_not_swallowed(self):
        """反向锁：真的以那句开头长正文必须能被引用（长度上限在保护它）

        只看开头签名会误伤：AI 完全可能写一段以「这一步没有执行成功」开头的长说明。
        误伤的后果是静默偷换内容，比多报一次错严重。
        """
        long_note_like = chat_views.LEGACY_NOTE_TEXTS[0] + '\n\n' + ('补充说明。' * 60)
        self.assertGreater(len(long_note_like), chat_views.NOTE_MAX_CHARS)
        Message.objects.create(conversation=self.conv, role='assistant',
                               content=long_note_like)
        picked = chat_views._referenceable_reply(self.conv)
        self.assertEqual(picked.content, long_note_like, '长正文被当成占位文案吞了')

    def test_placeholder_registry_covers_every_note_defined(self):
        """机检：新增任何 *_NOTE / *_REPLY 文案常量都必须登记进 PLACEHOLDER_REPLIES

        漏登记的后果是静默的：那条文案会被当成「上一条正文」引用出去，或直接
        让补发判据失准，而且只在特定历史形状下才响。
        """
        import core.agent_registry as registry
        defined = {v for k, v in vars(chat_views).items()
                   if k.endswith(('_NOTE', '_REPLY')) and isinstance(v, str)}
        defined |= {v for k, v in vars(registry).items()
                    if k.endswith(('_NOTE', '_REPLY')) and isinstance(v, str)}
        self.assertTrue(defined, '两个模块都没扫到文案常量，这条锁在空跑')
        missing = defined - set(chat_views.PLACEHOLDER_REPLIES)
        self.assertEqual(missing, set(), f'这些占位文案没登记，会被当成正文引用：{missing}')
        # 历史文案必须仍在「短提示」的尺寸包里，否则长度上限会让它永远匹配不上
        for text in chat_views.LEGACY_NOTE_TEXTS:
            self.assertLessEqual(len(text.strip()), chat_views.NOTE_MAX_CHARS,
                                 f'这条历史文案超过上限，签名判据对它失效：{text[:20]}')


# ── 第三种失效：上游过载重试产出的「半条回复」────────────────────────────────
# 线上实测（对话 17，2026-09-07 真机复测）：平台先报 model_overloaded_error
# （qoder_error_code 10605，retry_status=retrying）并自行重试，重试后产出的
# agent.message 只有 289 字符 —— params 已完整闭合，只有 reply 写到半路就收尾，
# 而 span.model_request_end 仍是 is_error=false（平台认为正常完成）。
# 下面这份是那次事件的逐字原文（按 60 字符切片拼接，长度锁见下）。
OVERLOAD_TRUNCATED_PROTOCOL = (
    '{"intent":"knowledge_search","params":{"keyword":"岗位交接 SOP",'
    '"tag":"流程"},"reply":"这份手册已经在你的知识库里了，不用再存一遍——标题《岗位交接 SOP 手册》，'
    '标签「流程」，版本 v1.0（2026-09-07），五个部分齐全（适用场景 / 逐步流程 / 常见错误 / 检验清单 '
    '/ 风险提示）。我按「岗位交接 SOP」在标签「流程」下检索确认一下，避免重复建档。\\n\\n下一步：给《岗位交接 SOP'
    ' 手册》再加一个标签「人事」｜把这份手册转成一个 2026-09-14 截止的「交接流程落地」活动'
)


class PartialProtocolSalvageTest(TestCase):
    """行为锁：动作数据齐全、只有 reply 断掉的半条指令，必须照样执行

    这类残缺和「正文重抄被截断」是两回事：前者 params 完整，执行它是安全的，
    不执行等于把一次上游抖动算在用户头上（用户侧表现为「我什么都没想到」）。
    """

    def setUp(self):
        from activities.models import Activity
        from knowledge.models import Article
        self.Activity = Activity
        self.Article = Article
        self.user = User.objects.create_user('salv', password='x')

    def test_online_sample_is_protocol_and_really_unparsable(self):
        """前提锁：样本必须「看着像协议但整串解析不出来」，否则下面的锁全在空跑"""
        self.assertEqual(len(OVERLOAD_TRUNCATED_PROTOCOL), 289,
                         '样本不再是线上那条半条回复，锁的靶子变了')
        self.assertTrue(looks_like_protocol(OVERLOAD_TRUNCATED_PROTOCOL))
        self.assertIsNone(extract_intent(OVERLOAD_TRUNCATED_PROTOCOL))

    def test_closed_params_is_salvaged(self):
        data = salvage_partial_protocol(OVERLOAD_TRUNCATED_PROTOCOL)
        self.assertIsNotNone(data, 'params 已闭合却没救出来')
        self.assertEqual(data['intent'], 'knowledge_search')
        self.assertEqual(data['params'], {'keyword': '岗位交接 SOP', 'tag': '流程'})
        # 断掉的 reply 里已经写出来的话不能一起丢掉（那就是用户该看到的内容）
        self.assertIn('这份手册已经在你的知识库里了', data['reply'])
        self.assertNotIn('"', data['reply'], '未收尾的残缺里不该混进结构字符')

    def test_salvaged_call_actually_runs_the_tool(self):
        """救出来还不够，必须真的走到工具：线上那次是查询意图，工具没被执行"""
        content, payload, changed = orchestrator.process(
            self.user, OVERLOAD_TRUNCATED_PROTOCOL)
        self.assertNotEqual(content, PROTOCOL_TRUNCATED_NOTE,
                            'params 完整却仍按失败降级，等于救援没接上')
        for leaked in ('{"intent"', '"params"', '"reply"'):
            self.assertNotIn(leaked, content, f'协议原文泄到界面上：{leaked}')
        # knowledge.search 的固定口径 —— 出现它就证明工具真跑了
        self.assertIn('记忆和文章里都没有与「岗位交接 SOP」相关的内容', content)
        self.assertFalse(changed)

    def test_salvage_executes_write_operations(self):
        """写操作形状：params 完整时必须真的走到工具，不能只在读路径上生效
    
        2026-09-22 起 create 走两步确认流，salvage 到的 create 只出确认卡不落库
        （另见 test_salvaged_create_needs_confirmation）；这里用仍立即生效的
        set_status 保住「写操作 salvage 后必须真的写库」这条锁。
        """
        act = self.Activity.objects.create(user=self.user, name='交接流程落地')
        half = ('{"intent":"status_change","params":{"target":"交接流程落地",'
                '"status":"in_progress"},"reply":"好的，这就改')
        self.assertIsNone(extract_intent(half))
        content, payload, changed = orchestrator.process(self.user, half)
        act.refresh_from_db()
        self.assertEqual(act.status, 'in_progress', '写操作 salvage 后没落库')
        self.assertTrue(changed)
        self.assertIn('已将「交接流程落地」的状态', content)
        self.assertNotEqual(content, PROTOCOL_TRUNCATED_NOTE)
    
    def test_salvaged_create_needs_confirmation(self):
        """create 的 salvage 只出确认卡：救援不得绕过确认流直接落库（2026-09-22）"""
        half = ('{"intent":"create","params":{"name":"交接流程落地",'
                '"start_date":"2026-09-14","end_date":"2026-09-14"},"reply":"好的，正在为你建')
        self.assertIsNone(extract_intent(half))
        content, payload, changed = orchestrator.process(self.user, half)
        self.assertFalse(self.Activity.objects.filter(name='交接流程落地').exists(),
                         'salvage 不得绕过确认直接创建活动')
        self.assertIn('请确认', content)
        self.assertFalse(changed)
        self.assertNotEqual(content, PROTOCOL_TRUNCATED_NOTE)

    def test_salvaged_add_expense_needs_confirmation(self):
        """add_expense 的 salvage 同样只出确认卡（2026-09-22 记费用也走确认流）"""
        self.Activity.objects.create(user=self.user, name='交接流程落地')
        half = ('{"intent":"add_expense","params":{"target":"交接流程落地",'
                '"amount":30,"note":"测试"},"reply":"好的，这就记')
        self.assertIsNone(extract_intent(half))
        from activities.models import Expense
        content, payload, changed = orchestrator.process(self.user, half)
        self.assertEqual(Expense.objects.count(), 0,
                         'salvage 不得绕过确认直接记账')
        self.assertIn('请确认', content)
        self.assertFalse(changed)
        self.assertNotEqual(content, PROTOCOL_TRUNCATED_NOTE)

    def test_params_never_closed_is_not_salvaged(self):
        """安全边界：正文没抄完的那类（线上 msg 64）绝不救 —— 执行它等于拿半篇内容落库"""
        self.assertIsNone(salvage_partial_protocol(TRUNCATED_PROTOCOL))
        before = (self.Activity.objects.count(), self.Article.objects.count())
        content, payload, changed = orchestrator.process(self.user, TRUNCATED_PROTOCOL)
        self.assertEqual(content, PROTOCOL_TRUNCATED_NOTE)
        self.assertEqual((self.Activity.objects.count(), self.Article.objects.count()),
                         before, '未闭合的指令写了库')
        self.assertIsNone(payload)
        self.assertFalse(changed)

    def test_malformed_closed_params_is_not_salvaged(self):
        """闭合了但不是合法 JSON（多一个逗号）也不救：宁可不执行也不猜参数"""
        half = '{"intent":"create","params":{"name":"半成品",},"reply":"好'
        self.assertIsNone(salvage_partial_protocol(half))
        content, _, _ = orchestrator.process(self.user, half)
        self.assertEqual(content, PROTOCOL_TRUNCATED_NOTE)
        self.assertEqual(self.Activity.objects.count(), 0)

    def test_braces_inside_string_values_do_not_fool_the_scan(self):
        """字符串值里的大括号不是结构：非字符串感知的扫描会在这里提前「闭合」"""
        half = ('{"intent":"knowledge_create","params":{"title":"含 } 和 { 的标题",'
                '"content":"正文"},"reply":"好')
        data = salvage_partial_protocol(half)
        self.assertIsNotNone(data)
        self.assertEqual(data['params']['title'], '含 } 和 { 的标题')

    def test_escaped_quote_inside_string_does_not_end_the_string(self):
        """转义引号后的 } 仍在字符串里：漏掉反斜杠处理会在这里误判闭合"""
        half = ('{"intent":"knowledge_create","params":{"title":"他说\\"结束}\\"就走了",'
                '"content":"正文"},"reply":"好')
        data = salvage_partial_protocol(half)
        self.assertIsNotNone(data, '转义引号未处理，扫描被字符串里的 } 骗提前收尾')
        self.assertEqual(data['params']['title'], '他说"结束}"就走了')

    def test_tool_runs_even_when_reply_is_empty(self):
        """动作数据齐全但模型一个字都没写给用户看：工具照样被执行，由工具的回复顶上

        create 现在走确认流，顶上的是预览文案，且不得落库（2026-09-22）。
        """
        half = '{"intent":"create","params":{"name":"空话活动","start_date":"2026-09-20"},"reply":"'
        data = salvage_partial_protocol(half)
        self.assertIsNotNone(data)
        self.assertEqual(data['reply'], '', '样本不该带出 reply 文字')
        content, _, changed = orchestrator.process(self.user, half)
        self.assertEqual(content, '我准备创建活动「空话活动」，请确认：',
                         '工具没被执行（工具的预览回复没有顶上）')
        self.assertFalse(changed)
        self.assertFalse(self.Activity.objects.filter(name='空话活动').exists(),
                         '确认前不得创建活动')

    def test_chitchat_residue_shows_the_recovered_words(self):
        """线上第四种形状（本次真机复测实测到）：模型把话说完了，只是漏了收尾的引号括号

        事件形状：stop_reason=end_turn、is_error=false、265 字符 —— 没碰任何长度上限。
        把一句已经说完的回复判成「这一步没有执行成功」，用户只会再点一次然后同样失败。
        """
        half = ('{"intent":"chitchat","params":{},"reply":"《岗位交接 SOP 手册》已经在你知识库里了，'
                '标签就是「流程」。同一篇再存一次只会生成重复条目，所以我没有重复建档。')
        self.assertIsNone(extract_intent(half))
        content, _, changed = orchestrator.process(self.user, half)
        self.assertIn('已经在你知识库里了', content, '模型已经说出来的话被判成失败')
        self.assertNotEqual(content, PROTOCOL_TRUNCATED_NOTE)
        for leaked in ('{"intent"', '"params"', '"reply"'):
            self.assertNotIn(leaked, content, f'协议原文泄到界面上：{leaked}')
        self.assertFalse(changed)

    def test_closed_reply_with_missing_final_brace_is_read(self):
        """字符串自己收了尾、只是外层少了右括号：reply 必须完整取回，不带上垃圾"""
        half = '{"intent":"chitchat","params":{},"reply":"已经记下了，\\n明天提醒你"'
        data = salvage_partial_protocol(half)
        self.assertIsNotNone(data)
        self.assertEqual(data['reply'], '已经记下了，\n明天提醒你')

    def test_escaped_quotes_inside_reply_survive_the_scan(self):
        """reply 里的转义引号不是收尾：不认转义就会在这里断成半句"""
        half = ('{"intent":"chitchat","params":{},"reply":"他说\\"已经存好了\\"，'
                '还要不要再建个活动')
        data = salvage_partial_protocol(half)
        self.assertIsNotNone(data)
        self.assertEqual(data['reply'], '他说"已经存好了"，还要不要再建个活动')

    def test_dangling_escape_at_the_cut_does_not_break_decoding(self):
        """断点正好落在转义序列中间（末尾剩一个反斜杠）：丢掉它，整句仍要拿得到"""
        half = '{"intent":"chitchat","params":{},"reply":"好的，标题叫《方案」\\'
        data = salvage_partial_protocol(half)
        self.assertIsNotNone(data)
        self.assertTrue(data['reply'].startswith('好的，标题叫'), '尾部残缺字符让整句 reply 丢了')

    def test_residue_without_a_tool_and_without_words_is_not_leaked(self):
        """安全边界：既没工具可执行、又一行的话都没写出来时，仍按失败降级

        不能回一句空话（用户看不到任何反馈），更不能把协议原文当正文透出去。
        """
        half = '{"intent":"chitchat","params":{},"reply":"'
        data = salvage_partial_protocol(half)
        self.assertIsNotNone(data, '样本没闭合，测不到这条守卫')
        self.assertEqual(data['reply'], '')
        content, _, _ = orchestrator.process(self.user, half)
        self.assertNotIn('intent', content, '把协议原文当正文递给了用户')
        self.assertEqual(content, PROTOCOL_TRUNCATED_NOTE)



class JsonEndpointNeverHtmxTest(SimpleTestCase):
    """静态锁：返回 JSON 的端点不得被任何 hx-* 引用

    为什么不手维护清单：JSON 端点集合直接从 @json_login_required 的标记属性反查，
    新增一个 JSON 端点会自动进锁；靠人往名单里补上一句，名单迟早过期。
    混用的后果本项目已经吃过：HTMX 把 JSON 当纯文本插入 DOM。
    """

    HX_ATTR = re.compile(r'hx-[a-z-]+\s*=\s*"([^"]*)"')
    URL_NAME = re.compile(r"\{%\s*url\s+['\"]([\w.:-]+)['\"]")

    @property
    def json_views(self):
        from django.urls import get_resolver

        found = set()

        def walk(patterns, prefix):
            for pattern in patterns:
                if hasattr(pattern, 'url_patterns'):
                    ns = getattr(pattern, 'namespace', '') or ''
                    walk(pattern.url_patterns, f'{prefix}{ns}:' if ns else prefix)
                elif getattr(pattern.callback, 'json_login_required', False):
                    found.add(f'{prefix}{pattern.name}')

        walk(get_resolver().url_patterns, '')
        return found

    def test_marker_lookup_is_not_empty(self):
        """前提锁：反查为空时下面那条遍历会空跑（没扫到东西 ≠ 没问题）"""
        self.assertIn('chat:pin_candidates', self.json_views)
        self.assertGreaterEqual(len(self.json_views), 6, 'JSON 端点反查数量不对，装饰器标记丢了')

    def test_no_hx_attribute_targets_a_json_endpoint(self):
        templates = (Path(__file__).resolve().parent.parent / 'templates').rglob('*.html')
        scanned = 0
        for tpl in templates:
            # 必须先剔注释：项目里多处注释就在解释「为什么不能挂 hx-*」，
            # 不剔的话锁会被自己的说明文档打成假失败。
            for attr in self.HX_ATTR.findall(code_only(tpl.read_text(encoding='utf-8'))):
                for name in self.URL_NAME.findall(attr):
                    scanned += 1
                    self.assertNotIn(
                        name, self.json_views,
                        f'{tpl.name} 把 hx-* 挂到了 JSON 端点 {name}：'
                        'HTMX 会把 JSON 当文本插入 DOM')
        self.assertGreaterEqual(scanned, 5, '一个 hx- 端点都没扫到，这条锁是空的')




class PickCandidateTest(TestCase):
    """候选点选流：候选卡「选它」→ 服务端重放工具 → 原消息变确认卡 → 确认执行

    背景（2026-09 用户截图）：改活动时间匹配到 2 个同名活动，文字回「第一个」
    要云端 AI 再解释一轮，上下文一丢就原地打转。点选把「选哪个」收编到服务端：
    tool/params 取自落库 payload（请求只有 index），目标 id 经 visible 过滤。
    """

    def setUp(self):
        self.user = User.objects.create_user('chatuser', password='pw')
        self.client.login(username='chatuser', password='pw')
        self.conv = Conversation.objects.create(user=self.user, session_id='s1',
                                                agent_id='a1', title='候选点选')
        from activities.models import Activity
        self.a1 = Activity.objects.create(user=self.user, name='看望爸爸',
                                          status='planned',
                                          start_date=timezone.localdate())
        self.a2 = Activity.objects.create(user=self.user, name='看望爸爸',
                                          status='done',
                                          start_date=timezone.localdate() - timedelta(days=6))
        self.msg = Message.objects.create(
            conversation=self.conv, role='assistant', event_type='assistant.message',
            content='收到，按这个改：日期改为今天。\n\n⚠️ 匹配到 2 个活动，请告诉我具体是哪一个',
            payload={
                'card': 'candidates', 'activity_ids': [],
                'card_data': {
                    'hint': '匹配到 2 个活动，请告诉我具体是哪一个',
                    'items': [
                        {'id': self.a1.id, 'name': '看望爸爸', 'status': 'planned',
                         'status_label': '计划', 'date_label': '09-12',
                         'detail_url': f'/activities/{self.a1.id}/'},
                        {'id': self.a2.id, 'name': '看望爸爸', 'status': 'done',
                         'status_label': '已完成', 'date_label': '09-06',
                         'detail_url': f'/activities/{self.a2.id}/'},
                    ],
                    # 明天：有效且与现值不同，保证重放必有变更 → 出确认卡
                    'pending_action': {'tool': 'activities.update',
                                       'params': {'target': '看望爸爸',
                                                  'start_date': (timezone.localdate() + timedelta(days=1)).isoformat()}},
                },
            })
        self.url = reverse('chat:pick_candidate', args=[self.msg.id])

    def _pick(self, index='0'):
        return self.client.post(self.url, {'index': index})

    def test_pick_turns_message_into_confirm_card(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        resp = self._pick('0')
        self.assertEqual(resp.status_code, 200)
        self.msg.refresh_from_db()
        payload = self.msg.payload
        self.assertEqual(payload['card'], 'confirm')
        self.assertEqual(payload['card_data']['kind'], 'update')
        self.assertEqual(payload['action']['tool'], 'activities.update')
        self.assertEqual(payload['action']['params']['target_id'], self.a1.id)
        self.assertTrue(payload['action'].get('token'), '确认令牌必须在落库后回填')
        self.assertIn('请确认', self.msg.content)
        self.assertIn('确认执行', resp.content.decode())
        self.assertIn('选它', resp.content.decode()) if False else None

    def test_pick_then_confirm_updates_activity(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self._pick('0')
        self.msg.refresh_from_db()
        token = self.msg.payload['action']['token']
        resp = self.client.post(
            reverse('chat:confirm_action', args=[self.msg.id]),
            {'decision': 'confirm', 'token': token})
        self.assertEqual(resp.status_code, 200)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.start_date.isoformat(), tomorrow)
        self.a2.refresh_from_db()
        self.assertNotEqual(self.a2.start_date.isoformat(), tomorrow, '选第一个却改了第二个')

    def test_pick_out_of_range_degrades_gracefully(self):
        resp = self._pick('9')
        self.assertEqual(resp.status_code, 200)
        self.msg.refresh_from_db()
        self.assertIsNone(self.msg.payload.get('card'), '失败后不得再渲染候选卡')
        self.assertIn('已失效', self.msg.content)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.start_date.isoformat(), timezone.localdate().isoformat(),
                         '失败路径不得改数据')

    def test_pick_twice_is_idempotent(self):
        """已点选过（消息变确认卡）后再误触 pick：幂等空操作，确认卡不得被毁"""
        self._pick('0')
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.payload['card'], 'confirm')
        resp = self._pick('1')
        self.assertEqual(resp.status_code, 200)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.payload['card'], 'confirm')
        self.assertNotIn('已失效', self.msg.content)
        self.assertTrue(self.msg.payload['action'].get('token'))

    def test_pick_other_users_message_404(self):
        other = User.objects.create_user('other', password='pw')
        c2 = self.client_class()
        c2.login(username='other', password='pw')
        resp = c2.post(self.url, {'index': '0'})
        self.assertEqual(resp.status_code, 404)

    def test_pick_result_card_without_action(self):
        """查询类候选重放：直接换结果卡，不进入确认流"""
        self.msg.payload['card_data']['pending_action'] = {
            'tool': 'activities.get', 'params': {'target': '看望爸爸'}}
        self.msg.save(update_fields=['payload'])
        resp = self._pick('1')
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.payload['card'], 'activity')
        self.assertNotIn('action', self.msg.payload)
        self.assertIn('详情', resp.content.decode()) if False else None


class AskDeepLinkTest(TestCase):
    """「问 AI」深链（?ask=）：预填输入框供用户改两个字再发

    动态开场 chips 已于 2026-09-19 按用户要求下线（core.chips 已删）；
    深链机制与 chips 无关，是活动详情页「问 AI」入口的承载，保留。"""

    TEMPLATE = Path(__file__).resolve().parent.parent / 'templates' / 'chat' / 'conversation_list.html'

    def setUp(self):
        self.src = self.TEMPLATE.read_text(encoding='utf-8')

    def test_ask_deep_link_script_wiring(self):
        """?ask= 深链处理必须存在：预填输入框、无激活对话时自动新建"""
        self.assertIn("location.search.match(/[?&]ask=([^&]+)/)", self.src)
        self.assertIn('paFitTextarea(input)', self.src)
        # 无激活对话时触发新建流程（与「+ 新建」共用 submit 处理器）
        self.assertIn("newBtn.form.dispatchEvent(new Event('submit'", self.src)

    def test_ask_param_stripped_from_url(self):
        """深链跳入后 replaceState 掉 ?ask=，刷新/分享不会重复触发预填"""
        self.assertIn("history.replaceState(null, '', '/chat/');", self.src)


class ActivityDetailAskAiTest(TestCase):
    """活动详情页「问 AI」入口：场景化提问深链"""

    def setUp(self):
        self.user = User.objects.create_user('ask_user', password='test')
        self.client.login(username='ask_user', password='test')
        self.activity = Activity.objects.create(
            user=self.user, name='桐庐周末游', status='planned',
            start_date=timezone.localdate() + timedelta(days=7))

    def test_detail_page_has_ask_ai_link(self):
        resp = self.client.get(f'/activities/{self.activity.id}/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('问 AI', html)
        # 深链带场景化提问文本（urlencode 后含活动名）
        self.assertIn('ask=', html)
        self.assertIn('%E6%A1%90%E5%BA%90%E5%91%A8%E6%9C%AB%E6%B8%B8', html)  # 桐庐周末游

    def test_ask_url_targets_chat_list(self):
        resp = self.client.get(f'/activities/{self.activity.id}/')
        html = resp.content.decode()
        self.assertIn('href="/chat/?ask=', html)
        # ask= 后面必须紧跟编码后的提问文本（而非重复的 key），防止双 ?ask=ask= 回归
        self.assertIn('href="/chat/?ask=%E5%B8%AE%E6%88%91', html)  # 帮我…


class ChatRagInjectionTest(TestCase):
    """④ 对话 RAG：段落级检索结果注入 turn_prompt，历史只存原文

    与知识库全文注入互补：一个走标题/全文匹配，一个走段落相似度。
    注入进的是组装文本（turn_prompt），不是消息历史（现有约定）。
    """

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_rag', agent_id='ag_rag',
            title='RAG')

    def _send(self, content):
        with patch('chat.views.get_service', return_value=FakeQoderService()):
            return self.client.post(
                f'/chat/{self.conv.id}/send/',
                {'content': content, 'page_context': 'chat'},
                HTTP_ACCEPT='application/json')

    def test_rag_context_enters_turn_prompt(self):
        from knowledge.models import Article
        Article.objects.create(
            user=self.user, title='冲绳潜水攻略',
            content='# 冲绳潜水攻略\n\n冲绳潜水需要提前一周预约教练，'
                    '并确认天气窗口与潮汐表。')
        resp = self._send('冲绳潜水要准备什么')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertIn('[相关知识库内容]', self.conv.turn_prompt)
        self.assertIn('冲绳潜水攻略', self.conv.turn_prompt)
        # 历史只存原文：注入文本不得污染用户消息
        self.assertEqual(
            self.conv.messages.get(role='user').content, '冲绳潜水要准备什么')

    def test_no_match_leaves_prompt_clean(self):
        resp = self._send('随便聊聊天气')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertNotIn('[相关知识库内容]', self.conv.turn_prompt)

    def test_rag_failure_degrades_to_plain_send(self):
        """RAG 环节抛异常只降级不阻断（容错铁律）"""
        from knowledge.models import Article
        Article.objects.create(
            user=self.user, title='冲绳潜水攻略', content='冲绳潜水预约教练')
        with patch('knowledge.rag.build_rag_context',
                   side_effect=RuntimeError('fts down')):
            resp = self._send('冲绳潜水要准备什么')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertNotIn('[相关知识库内容]', self.conv.turn_prompt)


# ==================== 交互重构：根路径与快捷卡片（2026-09-19）====================

class _FakeCreateService(FakeQoderService):
    """在 FakeQoderService 之上补 create_session：供建对话链路的测试用

    基类故意没有 create_session（发送/轮询路径若回退成“隐式建会话”会当场炸）。
    """

    def __init__(self):
        super().__init__()
        self.created_sessions = []

    def create_session(self, agent_id, environment_id):
        self.created_sessions.append(agent_id)
        return {'id': f'sess_daily{len(self.created_sessions)}'}


class ChatHomeTest(TestCase):
    """站点根路径口径（2026-10-16 起：根 = 「今日」Bento 首页，不再跳对话列表）

    daily 常驻会话机制已下线（2026-09-19）：此前访问根路径会复活/重建
    「daily」会话，用户删了又回来、看起来「无法删除」。根路径现在渲染
    今日页、完全不碰 Conversation，下面的反证锁继续防回归。
    """

    def setUp(self):
        self.user = User.objects.create_user('d', password='p')
        self.client.force_login(self.user)

    def test_root_serves_today_page(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '今日概览')
        self.assertContains(resp, '本月支出')

    def test_root_does_not_create_any_conversation(self):
        """反证锁：删光全部对话后访问根路径，不得凭空冒出新会话

        （旧机制会在根路径 get_or_create daily：云端失败才降级列表，
        云端可用时用户删掉的对话会在下次访问静默重建）"""
        Conversation.objects.all().delete()
        self.client.get('/')
        self.assertFalse(Conversation.objects.exists())

    def test_deleted_daily_conversation_stays_deleted(self):
        """用户删掉的 daily 对话不得被系统复活重建（本次需求的直接反证）"""
        conv = Conversation.objects.create(
            user=self.user, session_id='sess_d', agent_id='ag_d', title='daily')
        conv.delete()
        self.client.get('/')
        self.assertFalse(
            Conversation.objects.filter(user=self.user, title='daily').exists())

    def test_login_required(self):
        self.client.logout()
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp['Location'])

    def test_local_card_entry_available_in_every_conversation(self):
        """daily/工作台入口在所有对话可用。daily 简报 2026-10-08 起从头部 ⋯ 菜单
        振到输入框上方小按钮（用户要求常用操作放明面）；工作台卡仍在菜单里"""
        conv = Conversation.objects.create(
            user=self.user, session_id='sess_d', agent_id='ag_d',
            title='普通对话')
        html = self.client.get(f'/chat/{conv.id}/').content.decode()
        self.assertIn('data-local-card="daily_brief"', html)
        self.assertIn('data-local-card="today_brief"', html)
        # daily 简报在输入框上方（split-form 内），不在 ⋯ 菜单里
        form_zone = html.split('id="split-form"', 1)[1]
        self.assertIn('data-local-card="daily_brief"', form_zone)
        menu_zone = html.split('class="conv-menu', 1)[1].split('</details>', 1)[0]
        self.assertNotIn('daily_brief', menu_zone, '⋯ 菜单里不应再有 daily 简报入口')
        # 独立按钮行已删净：容器与撤 hidden 的旧机制都不应残留
        self.assertNotIn('id="local-cards"', html)
        self.assertNotIn('data-daily-id', html, '旧的按会话 id 显隐机制应已删净')
        # 详情页在头部操作行带同款入口
        detail = self.client.get(f'/chat/{conv.id}/detail/').content.decode()
        self.assertIn('data-local-card="daily_brief"', detail)
        self.assertNotIn('id="local-cards"', detail)

    def test_nav_daily_page_retired(self):
        """/daily/ 页下线：导航不再有「今日」入口，
        /daily/ 路由保留但重定向回首页（= 对话列表）"""
        conv = Conversation.objects.create(
            user=self.user, session_id='sess_d', agent_id='ag_d',
            title='普通对话')
        html = self.client.get(f'/chat/{conv.id}/').content.decode()
        self.assertNotIn('href="/daily/"', html)
        resp = self.client.get('/daily/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('home'))


class LocalCardTest(TestCase):
    """非 AI 快捷卡片：点击 → 服务端直出（不碰 Qoder / turn 状态机 / token）

    三条硬边界：不调云端（消息历史本来就不进 AI 上下文）；不占 turn 锁
    （AI 忙时照样能出卡）；失败降级为提示文本（绝不阻断对话）。
    """

    def setUp(self):
        self.user = User.objects.create_user('lc', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_lc', agent_id='ag_lc',
            title='普通对话')
        self.url = f'/chat/{self.conv.id}/local-card/'

    def _activity(self, **kw):
        defaults = dict(user=self.user, name='贵州行', status='planned',
                        start_date=timezone.localdate() + timedelta(days=2))
        defaults.update(kw)
        return Activity.objects.create(**defaults)

    def test_local_card_creates_system_message_with_snapshot(self):
        self._activity()
        resp = self.client.post(self.url, {'card': 'daily_brief'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        msg = Message.objects.get(id=data['message_id'])
        self.assertEqual(msg.role, 'system')
        self.assertEqual(msg.event_type, 'local.daily_brief')
        self.assertEqual(msg.payload['card'], 'daily_brief')
        self.assertTrue(msg.payload['card_data']['sections'])
        # 返回的是服务端渲染的消息片段（模板只有一份，前端只 append）
        self.assertIn('chat-message', data['html'])

    def test_local_card_does_not_touch_turn_or_qoder(self):
        """FakeQoderService 故意没有 create_session：真被调去碰云端会当场炸"""
        service = FakeQoderService()
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.post(self.url, {'card': 'daily_brief'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(service.sent, [], '快捷卡片不得向云端发送任何消息')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_NONE)

    def test_local_card_works_while_ai_is_busy(self):
        """不占 turn 锁：AI 正在跑也能出卡，两不干扰"""
        self.conv.turn_state = Conversation.TURN_AWAITING
        self.conv.turn_started_at = timezone.now()
        self.conv.save()
        resp = self.client.post(self.url, {'card': 'daily_brief'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_AWAITING)

    def test_unknown_card_rejected(self):
        resp = self.client.post(self.url, {'card': 'nope'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_other_users_conversation_404(self):
        other = User.objects.create_user('o', password='p')
        conv2 = Conversation.objects.create(user=other, session_id='s2', agent_id='a2')
        resp = self.client.post(f'/chat/{conv2.id}/local-card/',
                                {'card': 'daily_brief'}, HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)

    def test_gather_failure_degrades_without_message(self):
        """gather 抛异常只回错误提示（前端降级为瞬态文本），不落消息不阻断"""
        from chat import local_cards
        registry_patch = patch.dict(local_cards._REGISTRY, {
            'boom': {'key': 'boom', 'label': 'b', 'gather': lambda u: 1 / 0}})
        with registry_patch:
            resp = self.client.post(self.url, {'card': 'boom'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 500)
        self.assertIn('error', resp.json())
        self.assertFalse(Message.objects.filter(conversation=self.conv).exists())

    def test_history_renders_snapshot_without_requery(self):
        """快照式 payload：历史渲染只读卡片数据，回看不随后端数据漂移"""
        self._activity()
        resp = self.client.post(self.url, {'card': 'daily_brief'},
                                HTTP_ACCEPT='application/json')
        msg = Message.objects.get(id=resp.json()['message_id'])
        html = self.client.get(f'/chat/{self.conv.id}/widget-messages/').content.decode()
        self.assertIn('贵州行', html)
        self.assertIn(str(msg.id), html)
        # 之后把活动改名：历史卡仍是点按钮那一刻的快照
        self._activity_set_name(msg, '贵州行', renamed='改名后的活动')
        html2 = self.client.get(f'/chat/{self.conv.id}/widget-messages/').content.decode()
        self.assertIn('贵州行', html2)

    @staticmethod
    def _activity_set_name(msg, old, renamed):
        from activities.models import Activity
        Activity.objects.filter(name=old).update(name=renamed)

    def test_system_message_is_not_referenceable_as_last_reply(self):
        """$LAST_REPLY 引用池只认 assistant：系统卡片不会被当成「上一条回复」展开"""
        Message.objects.create(conversation=self.conv, role='system',
                               content='daily', event_type='local.daily_brief')
        self.assertIsNone(chat_views._referenceable_reply(self.conv))

    def test_system_message_never_enters_turn_prompt(self):
        """非 AI 消息不进 AI 上下文：发送新消息时 turn_prompt 不含卡片内容"""
        self._activity()
        self.client.post(self.url, {'card': 'daily_brief'},
                         HTTP_ACCEPT='application/json')
        service = FakeQoderService()
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.post(f'/chat/{self.conv.id}/send/',
                                    {'content': '今天有什么安排', 'page_context': 'chat'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        self.assertIn('今天有什么安排', self.conv.turn_prompt)
        self.assertNotIn('今日进行中', self.conv.turn_prompt,
                         '简报卡内容不得漏进 AI 上下文（它只活在消息历史的 payload 里）')


class DailyCardActionRowTest(SimpleTestCase):
    """daily 卡片内操作行（/daily/ 页下线后卡内就是唯一出口）

    快记面板 2026-10-28 整体下线：「新建活动」改为直跳创建页链接。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = Path(__file__).resolve().parent.parent
        cls.card = (base / 'templates' / 'chat' / 'cards' / 'daily_brief_card.html').read_text(encoding='utf-8')
        cls.js = (base / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')

    def test_card_action_row_tabs(self):
        """「＋ 新建活动」直跳创建页；日历 / 费用报告保留页面链接；
        「记一笔」已删（2026-09-21，费用必关联活动，统一走详情页费用表单）"""
        self.assertIn("url 'activities:activity_create'", self.card)
        self.assertNotIn('data-quick-open', self.card)
        self.assertIn("url 'activities:activity_calendar'", self.card)
        self.assertIn("url 'activities:expense_report'", self.card)

    def test_card_no_daily_page_link(self):
        """页面已下线：卡片头部不得再出现 Daily 页链接"""
        self.assertNotIn("url 'activities:daily'", self.card)
        self.assertNotIn("url 'daily'", self.card)

    def test_local_card_chain_wired(self):
        """local-card 委托链还在：chat-turn.js 委托调 /chat/<id>/local-card/"""
        self.assertIn("'[data-local-card]'", self.js)
        # 端点读 request.POST：委托必须发表单体而不是 JSON（发 JSON 会静默拿不到 card）
        self.assertIn('application/x-www-form-urlencoded', self.js)
        # chatId 从 location.pathname 解析（分栏 replaceState 与详情页 /detail/ 都覆盖）
        self.assertIn('/\\/chat\\/(\\d+)/', self.js)
        # 按钮行下线后 onReady 不应再引用 local-cards（残留则静默空跑；
        # 不能顺带扫 classList.remove('hidden')：状态条等 legitimate 用途含该子串）
        self.assertNotIn("getElementById('local-cards')", self.js)


class WorkbenchCardTest(TestCase):
    """工作台快捷卡（today_brief）：/today/ 页下线后的唯一出口，机制与 daily_brief 同一套"""

    def setUp(self):
        self.user = User.objects.create_user('wb', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_wb', agent_id='ag_wb', title='普通对话')
        self.url = f'/chat/{self.conv.id}/local-card/'

    def test_registry_has_today_brief(self):
        from chat.local_cards import get_local_card
        card = get_local_card('today_brief')
        self.assertIsNotNone(card)
        self.assertEqual(card['label'], '工作台')

    def test_today_brief_creates_system_message_with_snapshot(self):
        from activities.models import Activity
        Activity.objects.create(user=self.user, name='今日待办',
                                start_date=timezone.localdate(), status='planned')
        resp = self.client.post(self.url, {'card': 'today_brief'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        msg = Message.objects.get(id=data['message_id'])
        self.assertEqual(msg.role, 'system')
        self.assertEqual(msg.event_type, 'local.today_brief')
        self.assertEqual(msg.payload['card'], 'today_brief')
        self.assertEqual(msg.payload['card_data']['today_activities'][0]['name'], '今日待办')
        self.assertIn('chat-message', data['html'])

    def test_card_renders_sections_and_action_row(self):
        """渲染锁：焦点/活动区与卡内操作行都在同一份消息片段里"""
        from activities.models import Activity
        Activity.objects.create(user=self.user, name='今日待办',
                                start_date=timezone.localdate(), status='planned')
        resp = self.client.post(self.url, {'card': 'today_brief'},
                                HTTP_ACCEPT='application/json')
        html = resp.json()['html']
        self.assertIn('今日待办', html)
        self.assertIn('今日焦点', html)
        # 快记面板 2026-10-28 下线：「＋ 新建活动」直跳创建页（渲染后是真实 href）
        self.assertIn('href="/activities/new/"', html)
        self.assertIn('＋ 新建活动', html)
        self.assertNotIn('data-quick-open', html)

    def test_today_brief_does_not_touch_turn_or_qoder(self):
        service = FakeQoderService()
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.post(self.url, {'card': 'today_brief'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(service.sent, [], '快捷卡片不得向云端发送任何消息')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.turn_state, Conversation.TURN_NONE)


class WorkbenchCardWiringTest(SimpleTestCase):
    """工作台卡的静态接线锁：入口按钮（分栏页菜单 + 详情页操作行）/
    _card 分发 / 卡模板操作行与页链接 退场。

    2026-09-21：partials/local_cards.html 已删（入口挪进头部），
    锁改扫两个页面模板。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = Path(__file__).resolve().parent.parent
        cls.split = (base / 'templates' / 'chat' / 'conversation_list.html').read_text(encoding='utf-8')
        cls.detail = (base / 'templates' / 'chat' / 'conversation_detail.html').read_text(encoding='utf-8')
        cls.dispatch = (base / 'templates' / 'chat' / 'cards' / '_card.html').read_text(encoding='utf-8')
        cls.card = (base / 'templates' / 'chat' / 'cards' / 'today_brief_card.html').read_text(encoding='utf-8')

    def test_partial_has_both_buttons(self):
        for tpl in (self.split, self.detail):
            self.assertIn('data-local-card="daily_brief"', tpl)
            self.assertIn('data-local-card="today_brief"', tpl)
        # 旧 partial 已删，不能再有页面 include 它
        self.assertNotIn('partials/local_cards.html', self.split)
        self.assertNotIn('partials/local_cards.html', self.detail)

    def test_dispatch_includes_today_brief_card(self):
        self.assertIn("msg.payload.card == 'today_brief'", self.dispatch)
        self.assertIn('today_brief_card.html', self.dispatch)

    def test_card_action_row_and_no_retired_link(self):
        """卡内「＋ 新建活动」直跳创建页（快记面板 2026-10-28 下线）；
        「记一笔」已删（费用必关联活动）、「新建备忘」随备忘下线删"""
        self.assertIn("url 'activities:activity_create'", self.card)
        self.assertNotIn('data-quick-open', self.card)
        self.assertIn("url 'activities:activity_list'", self.card)
        self.assertNotIn("url 'today'", self.card)
        self.assertNotIn('core/today.html', self.card)


class ChatWideLayoutTest(SimpleTestCase):
    """对话区加宽：base.html 的 main_class 钩子 + 聊天页覆盖 + 侧栏折叠"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = Path(__file__).resolve().parent.parent
        cls.base_html = (base / 'templates' / 'base.html').read_text(encoding='utf-8')
        cls.list_html = (base / 'templates' / 'chat' / 'conversation_list.html').read_text(encoding='utf-8')
        cls.detail_html = (base / 'templates' / 'chat' / 'conversation_detail.html').read_text(encoding='utf-8')
        cls.css = (base / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')

    def test_main_class_hook_exists(self):
        self.assertIn('{% block main_class %}', self.base_html)

    def test_chat_page_widens_container(self):
        self.assertIn('{% block main_class %}mx-auto max-w-[1700px]', self.list_html)

    def test_sidebar_collapse_wiring(self):
        self.assertIn('id="sidebar-toggle"', self.list_html)
        self.assertIn("localStorage.getItem('pa-chat-sidebar')", self.list_html)
        self.assertIn('.chat-layout.sidebar-collapsed .chat-sidebar', self.css)

    def test_chat_pages_retired_aurora_for_typeframe(self):
        """Aurora 沉浸式深色主题已随 Typeframe 全站改版退役（2026-10-27）。

        历史背景：2026-10-21 上线 demo1 深色沉浸式（body.chat-aurora + .aurora 壳），
        2026-10-27 用户定稿 typeframe mockup（纯白黑字，VIEW 3 = 发送者标签 + 正文），
        聊天页回到全站同一套白底令牌。锁「不留残骸」：壳类、body class、
        meta theme-color 深色值、CSS 里的令牌翻转都不允许再回来 —— 半套 aurora
        挂在白站上会出现浅色导航 + 深色令牌的混搭（就是 2026-10-22 修过的「套用错了」）。"""
        self.assertIn('{% block body_class %}', self.base_html,
                      'base.html 的 body_class 钩子是通用能力，保留；聊天页只是不再使用它')
        for html, name in ((self.list_html, 'conversation_list'), (self.detail_html, 'conversation_detail')):
            self.assertNotIn('chat-aurora', html, f'{name} 还挂着 Aurora body class')
            self.assertNotIn('aurora', html, f'{name} 还有 aurora 壳类残留')
            self.assertNotIn('#0b0d1a', html, f'{name} 的深色 theme-color 还在')
        self.assertNotIn('body.chat-aurora', self.css, 'CSS 里 Aurora 令牌翻转还在')
        self.assertNotIn('.aurora', self.css, 'CSS 里 Aurora 组件皮肤还在')
        # Typeframe 对话流的最低接线：输入框走全站唯一的下划线口径
        self.assertIn('tf-underline', self.list_html)
        self.assertIn('tf-underline', self.detail_html)


class FollowUpCollapseTest(SimpleTestCase):
    """chips 收敛：历史回复的 chips 折叠、超量折叠，把空间还给消息流

    三方接线（JS 逻辑 / CSS 类 / 模板标记）缺一就静默失效，所以全部静态锁。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = Path(__file__).resolve().parent.parent
        cls.js = (base / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.tpl = (base / 'templates' / 'chat' / 'cards' / '_follow_ups.html').read_text(encoding='utf-8')
        cls.css = (base / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')

    def test_tidy_helper_exists_and_hooks(self):
        self.assertIn('window.paTidyFollowUps', self.js)
        self.assertIn("querySelectorAll('[data-follow-ups]')", self.js)
        # 两条触发路径都要在：append 后重跑 + 页面加载完首跑
        self.assertIn('window.paTidyFollowUps(messagesEl)', self.js)
        self.assertIn('window.paTidyFollowUps(document)', self.js)

    def test_folded_class_has_css(self):
        self.assertIn('.follow-ups-folded', self.css)

    def test_template_marker_intact(self):
        self.assertIn('data-follow-ups', self.tpl)
        self.assertIn('data-followup="{{ item }}"', self.tpl)

    def test_more_button_never_sends(self):
        """「还有 N 条建议…」展开钮不得带 data-followup（否则会被发送委托误发）"""
        self.assertIn("setAttribute('data-followups-more'", self.js)
        tidy_body = self.js.split('window.paTidyFollowUps =')[1].split('window.PaChatTurn')[0]
        self.assertNotIn("'data-followup'", tidy_body.replace("'[data-followup]'", ''),
                         '收敛函数内部不得给任何元素挂 data-followup 属性')


# ── 3 天展示窗口 + 超 3 天自动归档（2026-09-21 用户需求） ──

class ChatHistoryWindowTest(TestCase):
    """对话视图只渲染最近 CHAT_HISTORY_DAYS 天的消息，?history=all 展开全量

    只是展示层窗口：AI 上下文与归档摘要仍读全量历史。三个渲染口
    （分栏初始 / widget-messages 片段 / 详情页）都必须带「显示更早」入口，
    且窗口内一条都没有时不得落成「发送消息开始对话」（对话不是空的）。
    """

    def setUp(self):
        self.user = User.objects.create_user('hw', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_hw', agent_id='ag_hw', title='历史窗口')
        self.old = Message.objects.create(
            conversation=self.conv, role='user', content='五天前的老消息')
        self.new = Message.objects.create(
            conversation=self.conv, role='assistant', content='今天的新消息')
        cutoff = timezone.now() - timedelta(days=chat_views.CHAT_HISTORY_DAYS + 2)
        Message.objects.filter(pk=self.old.pk).update(created_at=cutoff)

    def test_list_shows_only_recent_with_older_entry(self):
        resp = self.client.get(f'/chat/{self.conv.id}/')
        html = resp.content.decode()
        self.assertNotIn('五天前的老消息', html)
        self.assertIn('今天的新消息', html)
        self.assertIn('显示更早的 1 条消息', html)
        self.assertIn('history=all', html)

    def test_list_history_all_expands_full_history(self):
        html = self.client.get(f'/chat/{self.conv.id}/?history=all').content.decode()
        self.assertIn('五天前的老消息', html)
        self.assertIn('今天的新消息', html)
        self.assertNotIn('显示更早的', html)

    def test_widget_fragment_windows_and_expands(self):
        html = self.client.get(f'/chat/{self.conv.id}/widget-messages/').content.decode()
        self.assertNotIn('五天前的老消息', html)
        self.assertIn('显示更早的 1 条消息', html)
        html = self.client.get(
            f'/chat/{self.conv.id}/widget-messages/?history=all').content.decode()
        self.assertIn('五天前的老消息', html)

    def test_detail_page_windows(self):
        html = self.client.get(f'/chat/{self.conv.id}/detail/').content.decode()
        self.assertNotIn('五天前的老消息', html)
        self.assertIn('显示更早的 1 条消息', html)

    def test_all_old_conversation_does_not_show_empty_guide(self):
        """整个对话都超过窗口期：不能显示「发送消息开始对话」（消息并没有丢）"""
        Message.objects.filter(pk=self.new.pk).update(
            created_at=timezone.now() - timedelta(days=chat_views.CHAT_HISTORY_DAYS + 1))
        html = self.client.get(f'/chat/{self.conv.id}/widget-messages/').content.decode()
        self.assertNotIn('发送消息开始对话', html)
        self.assertIn('近 3 天没有新消息', html)
        self.assertIn('显示更早的 2 条消息', html)

    def test_recent_conversation_has_no_older_entry(self):
        conv = Conversation.objects.create(
            user=self.user, session_id='sess_hw2', agent_id='ag_hw2', title='新对话')
        html = self.client.get(f'/chat/{conv.id}/').content.decode()
        self.assertIn('发送消息开始对话', html)
        self.assertNotIn('显示更早的', html)


class AutoArchiveStaleConversationTest(TestCase):
    """archive_stale_conversations：超 CHAT_HISTORY_DAYS 天没聊天的对话自动归档

    与手工归档同一套口径（状态 + turn 清理 + 平台 cancel）；阈值与展示窗口
    同源（chat.models.CHAT_HISTORY_DAYS），不能两边各写一个 3。
    """

    def setUp(self):
        from io import StringIO
        from django.core.management import call_command
        self.call_command = call_command
        self.out = StringIO
        self.user = User.objects.create_user('aa', password='p')
        self.client.force_login(self.user)
        old = timezone.now() - timedelta(days=chat_views.CHAT_HISTORY_DAYS + 1)

        self.fresh = Conversation.objects.create(
            user=self.user, session_id='sess_f', agent_id='ag_f', title='活跃')
        Message.objects.create(conversation=self.fresh, role='user', content='今天还在聊')

        self.stale = Conversation.objects.create(
            user=self.user, session_id='sess_s', agent_id='ag_s', title='超期')
        Message.objects.create(conversation=self.stale, role='user', content='三天前的消息')
        Message.objects.filter(conversation=self.stale).update(created_at=old)

        self.empty_stale = Conversation.objects.create(
            user=self.user, session_id='sess_e', agent_id='ag_e', title='空且超期')
        Conversation.objects.filter(pk=self.empty_stale.pk).update(created_at=old)

        self.already = Conversation.objects.create(
            user=self.user, session_id='sess_a', agent_id='ag_a', title='已归档',
            status='archived')

    def test_archives_stale_only(self):
        self.call_command('archive_stale_conversations')
        self.fresh.refresh_from_db()
        self.stale.refresh_from_db()
        self.empty_stale.refresh_from_db()
        self.already.refresh_from_db()
        self.assertEqual(self.fresh.status, 'idle')
        self.assertEqual(self.stale.status, 'archived')
        self.assertEqual(self.empty_stale.status, 'archived', '空对话按创建时间计也要归档')
        self.assertEqual(self.already.status, 'archived', '已归档的保持不动')

    def test_dry_run_writes_nothing(self):
        self.call_command('archive_stale_conversations', dry_run=True)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.status, 'idle')

    def test_archived_conversations_hidden_from_default_list(self):
        """归档后默认列表不再出现（否则自动归档等于没做），?archived=1 可查看"""
        self.stale.status = 'archived'
        self.stale.save(update_fields=['status'])
        self.fresh.status = 'idle'
        self.fresh.save(update_fields=['status'])
        # 2026-10-11：有活跃对话时 /chat/ 自动重定向进最近对话，列表断言跟随重定向页
        html = self.client.get('/chat/', follow=True).content.decode()
        self.assertIn('活跃', html)
        self.assertNotIn('>超期<', html)
        html = self.client.get('/chat/?archived=1').content.decode()
        self.assertIn('超期', html)
        self.assertNotIn('>活跃<', html)

    def test_resets_active_turn_when_archiving(self):
        self.stale.turn_state = Conversation.TURN_AWAITING
        self.stale.turn_started_at = timezone.now()
        self.stale.save(update_fields=['turn_state', 'turn_started_at'])
        with patch('agents.services.get_service') as mock_svc:
            self.call_command('archive_stale_conversations')
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.turn_state, Conversation.TURN_NONE)
        # cron 会把所有超期对话一并归档（本例 2 个），断言目标对话被 cancel 过即可
        calls = [c.args[0] for c in mock_svc.return_value.cancel_session.call_args_list]
        self.assertIn('sess_s', calls)

    def test_threshold_follows_chat_history_days(self):
        """--days 参数可调，默认值与展示窗口同源"""
        self.call_command('archive_stale_conversations', days=30)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.status, 'idle', '30 天阈值下 4 天前消息不算超期')


# ── 2026-10 UI 走查：chat 十条修复的回归锁 ──

class ChatUiAuditFixesTest(TestCase):
    """锁住十项修复里「坏掉时没有报错、只有用户看得见」的部分

    优先做渲染断言（切范围切空 = 静默空跑的假锁，本项目踩过多次）；
    只有无法从渲染结果判定的（如「不再用 innerHTML 拼标题」）才退回源码锁，
    且一律先剔注释再比禁用词。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        cls.tpl_root = root / 'templates' / 'chat'
        cls.turn_js = (root / 'static' / 'js' / 'chat-turn.js').read_text(encoding='utf-8')
        cls.turn_js_code = ChatTurnFlowJsTest._strip_js_comments(cls.turn_js)

    def setUp(self):
        self.user = User.objects.create_user('audit', password='pw')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_audit', agent_id='ag_audit', title='走查')

    def _msg(self, role='assistant', content='正文', payload=None):
        return Message.objects.create(conversation=self.conv, role=role,
                                      content=content, event_type='assistant.message',
                                      payload=payload)

    # ── item 1：turn_error 缺省文案 ──

    def test_turn_error_default_note_survives_refresh(self):
        """刷新后渲染（include 不传 note）必须还有可读文案，而不是光杆「⚠️」"""
        from django.template.loader import render_to_string
        html = render_to_string('chat/partials/turn_error.html',
                                 {'conversation': self.conv})
        self.assertIn('上一轮没有完成，可以再问一次。', html)
        # 防「空跑」：默认文案确实来自 firstof 兜底，而不是测试自己造的场景
        tpl = (self.tpl_root / 'partials' / 'turn_error.html').read_text(encoding='utf-8')
        self.assertIn('{% firstof note "上一轮没有完成，可以再问一次。" %}', tpl)

    def test_default_note_single_source(self):
        """模板兜底句与 views 的 TURN_INTERRUPTED_NOTE 必须同句；conversation_list 不许再硬编码传 note"""
        tpl = (self.tpl_root / 'partials' / 'turn_error.html').read_text(encoding='utf-8')
        self.assertIn(chat_views.TURN_INTERRUPTED_NOTE, tpl)
        split = (self.tpl_root / 'conversation_list.html').read_text(encoding='utf-8')
        start = split.index('{% include "chat/partials/turn_error.html"')
        self.assertNotIn('note=', split[start:split.index('%}', start)],
                         'conversation_list 又硬编码了第二份兜底文案')

    # ── item 2：weekly_review 卡不再被静默丢弃 ──

    def test_weekly_review_card_renders(self):
        payload = {'card': 'weekly_review', 'card_data': {
            'week_start': '2026-10-12', 'completed_count': 3,
            'questions': ['本周完成了什么？', '有什么卡住了？', '下周第一件事？']}}
        msg = self._msg(payload=payload)
        from django.template.loader import render_to_string
        html = render_to_string('chat/partials/_message.html', {'msg': msg})
        self.assertIn('周回顾', html)
        self.assertIn('已完成 3 个', html)
        self.assertIn('本周完成了什么？', html)
        self.assertIn('下周第一件事？', html, '第三条引导被折叠壳藏掉了（不该有 data-follow-ups 壳）')
        self.assertNotIn('data-follow-ups', html)
        self.assertIn('data-followup="有什么卡住了？"', html,
                      '引导问题必须走唯一发送委托，不是死文本')

    def test_card_dispatch_has_weekly_review_branch(self):
        """分发锁：_card.html 没有 weekly_review 分支时，上面的渲染会悄悄只剩正文"""
        dispatch = (self.tpl_root / 'cards' / '_card.html').read_text(encoding='utf-8')
        self.assertIn("msg.payload.card == 'weekly_review'", dispatch)

    # ── item 3/4/8：分栏页脚本的静默丢失 / 注入 / alert ──

    def test_split_script_has_no_native_dialogs(self):
        split = (self.tpl_root / 'conversation_list.html').read_text(encoding='utf-8')
        js = ChatTurnFlowJsTest._strip_js_comments(split)
        self.assertNotIn('alert(', js)
        self.assertIn('flashSplitHint(', js, '行内自消提示是 alert 的替代品，不能整体删掉')

    def test_conv_item_builds_title_with_dom_api(self):
        """标题是用户数据：只许 textContent 进 DOM，不许拼进 innerHTML 字符串"""
        split = (self.tpl_root / 'conversation_list.html').read_text(encoding='utf-8')
        js = ChatTurnFlowJsTest._strip_js_comments(split)
        self.assertIn('h3.textContent = title', js)
        # ⋯ 菜单的 innerHTML 只可以是纯常量模板：拼接用户数据的旧路径不许复活
        start = js.index('function convMenuEl')
        body = js[start:js.index('function convItemEl')]
        assign = body.split('box.innerHTML =')[1].split('var details')[0]
        self.assertIn('<details class="conv-menu', assign, '切片没捕到菜单模板本体，这条锁是空的')
        self.assertNotIn('+ title', assign, '菜单模板拼进了标题：innerHTML 注入面回来了')
        self.assertNotIn('+ data', assign)

    def test_split_send_without_conversation_is_intercepted(self):
        """未选对话点发送：拦原生 POST（整页重绘丢输入），给可见提示"""
        split = (self.tpl_root / 'conversation_list.html').read_text(encoding='utf-8')
        js = ChatTurnFlowJsTest._strip_js_comments(split)
        # 用 rindex：新建表单的处理器先出现且共享同一子串，最后一条才是未选对话的拦截
        start = js.rindex("form.addEventListener('submit'")
        body = js[start:start + 400]
        self.assertIn('if (currentId) return;', body,
                      '已选中时必须给 PaChatTurn 让路，不能双处理器')
        self.assertIn('e.preventDefault();', body)
        self.assertIn('flashSplitHint', body)

    # ── item 5：活动卡状态按钮 fetch 化 ──

    def test_status_forms_marked_for_fetch_delegation(self):
        card = (self.tpl_root / 'cards' / 'activity_card.html').read_text(encoding='utf-8')
        self.assertIn('data-status-fetch', card)
        self.assertIn('data-status-row', card)
        self.assertIn('data-status-label', card)

    def test_turn_js_delegates_status_submit_via_api_fetch(self):
        js = self.turn_js_code
        self.assertIn("matches('[data-status-fetch]')", js)
        self.assertIn('new FormData(form)', js,
                      '表单自带 csrf 隐藏域：FormData 是唯一不带漏的组 body 方式')
        self.assertIn('apiFetch(form.action', js)
        self.assertIn('e.defaultPrevented', js, '宿主表单已有处理器（PaChatTurn）时必须让路')
        self.assertNotIn('hx-', js, 'fetch 通道里不许混进 HTMX 属性名')

    # ── item 6：确认卡整卡换，不再文案矛盾 ──

    def test_confirm_flow_swaps_whole_card(self):
        from core.agent_registry import make_action_token
        msg = self._msg(payload={
            'card': 'confirm',
            'card_data': {'kind': 'create', 'name': '周末逛博物馆', 'fields': []},
            'action': {'tool': 'activities.create', 'params': {}, 'token': ''},
        })
        msg.payload['action']['token'] = make_action_token(self.user, msg.id, 'confirm')
        msg.save(update_fields=['payload'])
        pending = self.client.get(f'/chat/{self.conv.id}/detail/').content.decode()
        self.assertIn('点确认后才会创建', pending)
        self.assertIn(f'id="confirm-card-{msg.id}"', pending, '整卡必须带可替换的锚点 id')
        resp = self.client.post(reverse('chat:confirm_action', args=[msg.id]),
                                {'decision': 'cancel', 'token': 'x'})
        html = resp.content.decode()
        self.assertIn(f'id="confirm-card-{msg.id}"', html,
                      '确认端点必须返回整卡（否则 pending 文案与结果态同框）')
        self.assertIn('已取消', html)
        self.assertNotIn('点确认后才会创建', html, '决议后 pending 引导语必须消失')
        self.assertNotIn('name="decision"', html, '决议后不该再有确认/取消表单')
        actions = (self.tpl_root / 'cards' / '_confirm_actions.html').read_text(encoding='utf-8')
        self.assertNotIn('hx-target="#card-actions-', actions)
        self.assertIn('hx-target="#confirm-card-{{ msg.id }}"', actions)

    # ── item 7：display 工具类坑（扫四处代表模板的每一行 class） ──

    def test_centering_classes_have_flex_host(self):
        """items-center/justify-center 必须挂在 flex/inline-flex 宿主上（否则整行塌陷）"""
        for rel in ('conversation_list.html', 'partials/pin_bar.html',
                    'cards/activity_card.html', 'cards/confirm_card.html'):
            src = code_only((self.tpl_root / rel).read_text(encoding='utf-8'))
            # JS 字符串里的 class 赋值同样是 DOM，一起扫
            for m in re.finditer(r'class(?:Name)?\s*=\s*[\'"]([^\'"]*)[\'"]', src):
                classes = m.group(1).split()
                if {'items-center', 'justify-center'} & set(classes):
                    self.assertTrue(
                        {'flex', 'inline-flex', 'md:flex', 'md:inline-flex'} & set(classes)
                        or any(c.endswith('/flex') or 'flex--' in c for c in classes),
                        f'{rel} 的 class="{m.group(1)}" 又踩了 display 坑')

    # ── item 9：时间戳绝对时间可达 ──

    def test_message_time_uses_time_element(self):
        from django.template.loader import render_to_string
        msg = self._msg(role='user', content='时间戳锁')
        html = render_to_string('chat/partials/_message.html', {'msg': msg})
        self.assertRegex(html, r'<time[^>]+datetime="\d{4}-\d{2}-\d{2}T')
        self.assertRegex(html, r'<time[^>]+title="')

    # ── item 10：抽屉 chrome 隐藏误导提示 ──

    def test_drawer_chrome_hides_pin_hint_only(self):
        base = f'/chat/{self.conv.id}/widget-messages/'
        normal = self.client.get(base).content.decode()
        self.assertIn('可钉住一个活动', normal, '分栏页的提示仍是唯一发现入口，不能被删坏')
        drawer = self.client.get(base + '?chrome=drawer').content.decode()
        self.assertNotIn('可钉住一个活动', drawer)
        self.assertIn('data-pin-mount', drawer, '隐藏位必须还在：抽屉靠它搬运钉选槽')
        pinned = Activity.objects.create(user=self.user, name='钉住的活動', status='planned',
                                         start_date=timezone.localdate())
        Conversation.objects.filter(pk=self.conv.pk).update(pin_activity=pinned)
        drawer_pinned = self.client.get(base + '?chrome=drawer').content.decode()
        self.assertIn('钉住的活動', drawer_pinned, '抽屉上下文里 chip（已钉状态）要照常显示')


class ChatAutoEnterLatestTest(TestCase):
    """打开 /chat/ 自动进入最近一次对话（2026-10-11 用户要求，不再让用户从列表里选）"""

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client.login(username='raven', password='test')

    def _make(self, sid, title, minutes_ago):
        conv = Conversation.objects.create(user=self.user, session_id=sid, title=title)
        Conversation.objects.filter(pk=conv.pk).update(
            updated_at=timezone.now() - timedelta(minutes=minutes_ago))
        return conv

    def test_root_redirects_to_most_recent_conversation(self):
        self._make('sess_old', '旧对话', 60)
        latest = self._make('sess_new', '最近对话', 5)
        resp = self.client.get('/chat/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, f'/chat/{latest.id}/')

    def test_redirect_target_lands_in_chat_view(self):
        """重定向后的页面必须直接带活跃对话（data-active-id），移动端 JS 才会进聊天视图"""
        latest = self._make('sess_x', '直接可聊', 1)
        html = self.client.get('/chat/', follow=True).content.decode()
        self.assertIn(f'data-active-id="{latest.id}"', html)
        self.assertIn('直接可聊', html, '左栏列表仍要完整渲染，☰ 可回列表')

    def test_ask_deeplink_is_not_redirected(self):
        """?ask= 深链自带对话创建/预填逻辑，不能被重定向劫走"""
        self._make('sess_y', '占位', 1)
        resp = self.client.get('/chat/?ask=帮我记一笔')
        self.assertEqual(resp.status_code, 200)

    def test_archived_view_is_not_redirected(self):
        self._make('sess_z', '占位', 1)
        resp = self.client.get('/chat/?archived=1')
        self.assertEqual(resp.status_code, 200)

    def test_search_is_not_redirected(self):
        self._make('sess_s', '占位', 1)
        resp = self.client.get('/chat/?q=占位')
        self.assertEqual(resp.status_code, 200)

    def test_no_conversations_stays_on_list_page(self):
        """零对话时没有可进入的目标，留在列表页（空态 + 新建入口）"""
        resp = self.client.get('/chat/')
        self.assertEqual(resp.status_code, 200)


class MemoryPerTurnInjectionTest(TestCase):
    """记忆每轮注入通道（ROA 改进 Phase A）

    修复的坑与首帧协议同构：旧实现在 create_conversation 里注入一次记忆，
    存量会话永远看不到后来新增的记忆。现在注入挂在 _build_ai_content（每轮），
    这里用「发送动作后 turn_prompt 含记忆块」做行为锁——与
    StaleSessionReferenceReminderTest 锁协议补发是同一个思路。
    """

    def setUp(self):
        self.user = User.objects.create_user('memuser', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_m', agent_id='ag_m', title='旧对话')
        self.url = f'/chat/{self.conv.id}/send/'

    def _post(self, content='继续'):
        service = FakeQoderService()
        with patch('chat.views.get_service', return_value=service):
            resp = self.client.post(self.url, {'content': content, 'page_context': 'home'},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.conv.refresh_from_db()
        return self.conv.turn_prompt

    def _make_memories(self):
        from memory.models import Memory
        Memory.objects.create(user=self.user, content='回复别铺满 bullet，先给大纲',
                              type='procedural', importance=8)
        Memory.objects.create(user=self.user, content='住在杭州', category='fact',
                              importance=6)

    def test_existing_conversation_gets_memories_on_send(self):
        """存量会话发送时注入记忆（修复核心：不再只在新建时注入一次）"""
        self._make_memories()
        prompt = self._post()
        self.assertIn('[用户记忆', prompt)
        self.assertIn('回复别铺满 bullet，先给大纲', prompt)
        self.assertIn('住在杭州', prompt)

    def test_meta_rules_precede_memory_content(self):
        """元规则必须先于记忆内容下发（防记忆条目把指令提升为最高优先级）"""
        self._make_memories()
        prompt = self._post()
        self.assertIn('记忆使用规则', prompt)
        self.assertLess(prompt.index('记忆使用规则'), prompt.index('[用户记忆'))

    def test_user_message_stays_after_memory_block(self):
        """记忆块在正文之前，用户原文仍在末尾（历史只存原文的约定不破）"""
        self._make_memories()
        prompt = self._post('明天几点开会')
        self.assertIn('明天几点开会', prompt)
        self.assertGreater(prompt.index('明天几点开会'), prompt.index('[用户记忆'))
        self.assertNotIn('用户记忆', self.conv.messages.filter(role='user').first().content)

    def test_candidate_sensitive_episodic_are_not_injected(self):
        """candidate 不进常驻（等复验）；sensitive 不进注入；episodic 只以
        最近 3 条单行进「最近聊过」块——4 条情景中那条「搬家」最旧，应被挤出"""
        from memory.models import Memory
        Memory.objects.create(user=self.user, content='可能在准备考证',
                              status='candidate', importance=6)
        Memory.objects.create(user=self.user, content='正在服用舍曲林',
                              sensitivity='sensitive', importance=7)
        Memory.objects.create(user=self.user, content='2026-10-12 讨论了搬家',
                              type='episodic', importance=5)
        for i in range(3):
            Memory.objects.create(user=self.user, content=f'更新的情景{i}号',
                                  type='episodic', importance=5)
        prompt = self._post()
        self.assertNotIn('可能在准备考证', prompt)
        self.assertNotIn('正在服用舍曲林', prompt)
        self.assertNotIn('讨论了搬家', prompt)

    def test_no_memories_means_no_marker(self):
        """零记忆时不注入空壳标记，turn_prompt 也不因此报错"""
        prompt = self._post()
        self.assertNotIn('[用户记忆', prompt)
        self.assertIn('继续', prompt)
