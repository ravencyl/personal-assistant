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
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from chat import views as chat_views
from core.agent_registry import (INTENT_TOOL_MAP, build_protocol_prompt,
                                 extract_intent, orchestrator)

User = get_user_model()


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
    """超时链一致性：聊天等 AI < gunicorn 看门狗，否则响应途中 worker 被杀"""

    def test_ai_wait_shorter_than_gunicorn_timeout(self):
        deploy_md = (Path(__file__).resolve().parent.parent / 'DEPLOY.md').read_text(encoding='utf-8')
        m = re.search(r'--timeout (\d+)', deploy_md)
        self.assertIsNotNone(m, 'DEPLOY.md 必须写明 gunicorn --timeout')
        gunicorn_timeout = int(m.group(1))
        self.assertLess(chat_views.AI_WAIT_TIMEOUT, gunicorn_timeout,
                        '聊天等 AI 的上限必须小于 gunicorn --timeout')

    def test_no_hardcoded_wait_left(self):
        """所有等待都必须用 AI_WAIT_TIMEOUT，写死数字会让超时链再次倒挂"""
        src = (Path(__file__).resolve().parent / 'views.py').read_text(encoding='utf-8')
        hardcoded = re.findall(r'wait_for_response\([^)]*timeout=\d', src)
        self.assertEqual(hardcoded, [], f'写死的等待上限：{hardcoded}')


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
