"""对话协议 / 编排器的「通用问答逃生舱」测试

背景（线上真实故障）：首帧协议把 AI 钉成「只输出意图 JSON 的活动管理助手」，
于是「去美国出差要准备什么」这类问题被吸进 knowledge_search，本地没命中就直接回
「没有找到与…相关的知识库文章」，而云端 Agent 其实早就挂着 WebSearch/WebFetch 却从不被调用。

这里锁住四件事，防止再次退化：
1. 协议里必须有「通用问题→联网直答」的出口，且不再自称「活动管理助手」
2. 未注册工具的意图（ask）与纯自然语言回复都要能原样透传给用户
3. 含 `{}` 但无 intent 的自然语言不得被误当成协议 JSON
4. 等 AI 的超时必须显著小于 gunicorn --timeout（否则 worker 在响应途中被杀）
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
