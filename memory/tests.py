"""memory app 测试

覆盖：模型、服务层（检索/提取/注入/AI 存储）、Agent 工具、视图。
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from pathlib import Path

from unittest.mock import patch

from core.layout_asserts import assert_desktop_two_columns

from chat.models import Conversation, Message

from .models import Memory
from .services import (
    retrieve_memories, format_memory_for_injection,
    extract_memories_from_text, save_ai_extracted_memories,
    summarize_conversation_for_memory,
)

User = get_user_model()


class MemoryModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')

    def test_create_memory(self):
        m = Memory.objects.create(user=self.user, content='喜欢喝咖啡', category='preference', importance=6)
        self.assertEqual(str(m), '(偏好) 喜欢喝咖啡')
        self.assertEqual(m.importance, 6)
        self.assertEqual(m.access_count, 0)

    def test_default_values(self):
        m = Memory.objects.create(user=self.user, content='测试记忆')
        self.assertEqual(m.category, 'other')
        self.assertEqual(m.importance, 5)
        self.assertIsNone(m.source_message)


class RetrieveMemoriesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')
        Memory.objects.create(user=self.user, content='喜欢喝咖啡', category='preference', importance=8)
        Memory.objects.create(user=self.user, content='在杭州工作', category='fact', importance=5)
        Memory.objects.create(user=self.user, content='目标是跑马拉松', category='goal', importance=7)

    def test_retrieve_top_n(self):
        results = retrieve_memories(self.user, limit=2)
        self.assertEqual(len(results), 2)
        # 按 importance 降序
        self.assertEqual(results[0].content, '喜欢喝咖啡')
        self.assertEqual(results[1].content, '目标是跑马拉松')

    def test_retrieve_with_query(self):
        results = retrieve_memories(self.user, query='咖啡')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content, '喜欢喝咖啡')

    def test_retrieve_updates_access_count(self):
        retrieve_memories(self.user, limit=10)
        m = Memory.objects.get(user=self.user, content='喜欢喝咖啡')
        self.assertEqual(m.access_count, 1)
        self.assertIsNotNone(m.last_accessed)


class FormatMemoryTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')

    def test_format_empty(self):
        self.assertEqual(format_memory_for_injection([]), '')

    def test_format_with_memories(self):
        memories = [
            Memory(user=self.user, content='喜欢喝咖啡', category='preference'),
            Memory(user=self.user, content='在杭州工作', category='fact'),
        ]
        result = format_memory_for_injection(memories)
        self.assertIn('[用户记忆', result)
        self.assertIn('(偏好) 喜欢喝咖啡', result)
        self.assertIn('(事实) 在杭州工作', result)


class ExtractMemoriesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')

    def test_extract_preference(self):
        memories = extract_memories_from_text(self.user, '我喜欢喝咖啡')
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].category, 'preference')
        self.assertIn('咖啡', memories[0].content)

    def test_extract_goal(self):
        memories = extract_memories_from_text(self.user, '我的目标是今年读完20本书')
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].category, 'goal')

    def test_extract_multiple(self):
        memories = extract_memories_from_text(self.user, '我喜欢喝咖啡，我的目标是今年读完20本书')
        self.assertEqual(len(memories), 2)
        categories = {m.category for m in memories}
        self.assertIn('preference', categories)
        self.assertIn('goal', categories)

    def test_no_duplicate(self):
        extract_memories_from_text(self.user, '我喜欢喝咖啡')
        memories = extract_memories_from_text(self.user, '我喜欢喝咖啡')
        # 第二次应该被去重跳过
        self.assertEqual(len(memories), 0)

    def test_empty_text(self):
        memories = extract_memories_from_text(self.user, '')
        self.assertEqual(len(memories), 0)

    def test_extract_habit(self):
        memories = extract_memories_from_text(self.user, '我每天跑步5公里')
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].category, 'habit')


class SaveAIExtractedTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')

    def test_save_valid(self):
        memory_list = [
            {'content': '喜欢写代码', 'category': 'preference', 'importance': 7},
            {'content': '住在北京', 'category': 'fact', 'importance': 6},
        ]
        created = save_ai_extracted_memories(self.user, memory_list)
        self.assertEqual(len(created), 2)
        self.assertEqual(Memory.objects.filter(user=self.user).count(), 2)

    def test_save_invalid_category(self):
        memory_list = [{'content': '测试', 'category': 'invalid_cat', 'importance': 5}]
        created = save_ai_extracted_memories(self.user, memory_list)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].category, 'other')

    def test_save_clamps_importance(self):
        memory_list = [{'content': '测试', 'category': 'fact', 'importance': 99}]
        created = save_ai_extracted_memories(self.user, memory_list)
        self.assertEqual(created[0].importance, 10)

    def test_save_empty_list(self):
        created = save_ai_extracted_memories(self.user, [])
        self.assertEqual(len(created), 0)


@override_settings(ROOT_URLCONF='personal_assistant.urls')
class AgentToolMemorySearchTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')
        Memory.objects.create(user=self.user, content='喜欢喝咖啡', category='preference', importance=6)
        Memory.objects.create(user=self.user, content='在杭州工作', category='fact', importance=5)

    def test_memory_search_tool(self):
        from core.agent_registry import get_tool
        tool = get_tool('memory.search')
        self.assertIsNotNone(tool)
        result = tool['fn'](self.user, {'query': '咖啡'})
        self.assertIn('咖啡', result['reply'])

    def test_memory_search_no_results(self):
        from core.agent_registry import get_tool, ToolError
        tool = get_tool('memory.search')
        with self.assertRaises(ToolError):
            tool['fn'](self.user, {'query': ''})


@override_settings(ROOT_URLCONF='personal_assistant.urls')
class MemoryViewsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')
        self.m1 = Memory.objects.create(user=self.user, content='喜欢喝咖啡', category='preference', importance=6)
        self.m2 = Memory.objects.create(user=self.user, content='在杭州工作', category='fact', importance=5)

    def test_memory_list_page(self):
        resp = self.client.get(reverse('memory:memory_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '喜欢喝咖啡')
        self.assertContains(resp, '在杭州工作')

    def test_memory_list_search(self):
        resp = self.client.get(reverse('memory:memory_list') + '?q=咖啡')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '喜欢喝咖啡')
        self.assertNotContains(resp, '在杭州工作')

    def test_memory_list_category_filter(self):
        resp = self.client.get(reverse('memory:memory_list') + '?category=preference')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '喜欢喝咖啡')
        self.assertNotContains(resp, '在杭州工作')

    def test_memory_edit_get(self):
        resp = self.client.get(reverse('memory:memory_edit', args=[self.m1.id]))
        self.assertEqual(resp.status_code, 200)

    def test_memory_edit_post(self):
        resp = self.client.post(reverse('memory:memory_edit', args=[self.m1.id]), {
            'content': '喜欢喝茶',
            'category': 'preference',
            'importance': '7',
        })
        # 非 HTMX 请求 POST 后重定向到列表页
        self.assertEqual(resp.status_code, 302)
        self.m1.refresh_from_db()
        self.assertEqual(self.m1.content, '喜欢喝茶')
        self.assertEqual(self.m1.importance, 7)

    def test_memory_delete(self):
        resp = self.client.post(reverse('memory:memory_delete', args=[self.m1.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Memory.objects.filter(id=self.m1.id).exists())

    def test_memory_list_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse('memory:memory_list'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp.url)

    def test_user_isolation(self):
        """超级用户可以看到全部，普通用户只能看自己的"""
        other_user = User.objects.create_user('other', password='otherpass')
        Memory.objects.create(user=other_user, content='别人的记忆', category='fact')

        # 普通用户只能看自己的
        resp = self.client.get(reverse('memory:memory_list'))
        self.assertNotContains(resp, '别人的记忆')

        # 超级用户能看全部
        superuser = User.objects.create_superuser('admin', password='adminpass')
        self.client.login(username='admin', password='adminpass')
        resp = self.client.get(reverse('memory:memory_list'))
        self.assertContains(resp, '别人的记忆')

    def test_superuser_can_edit_and_delete_other_users_memory(self):
        """管理动作跟“超管见全部”同一口径（不再只限于自己的行）"""
        other = User.objects.create_user('other2', password='otherpass')
        m = Memory.objects.create(user=other, content='待整理的记忆', category='fact')
        User.objects.create_superuser('admin2', password='adminpass2')
        self.client.login(username='admin2', password='adminpass2')

        resp = self.client.post(reverse('memory:memory_edit', args=[m.id]), {
            'content': '已整理', 'category': 'fact', 'importance': '6',
        })
        self.assertEqual(resp.status_code, 302)
        m.refresh_from_db()
        self.assertEqual(m.content, '已整理')

        resp = self.client.post(reverse('memory:memory_delete', args=[m.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Memory.objects.filter(id=m.id).exists())

    def test_non_owner_gets_404_not_403(self):
        """无权访问统一 404（AGENTS.md），原来的手写 403 JSON 是第四套写法"""
        User.objects.create_user('stranger', password='strangerpass')
        self.client.login(username='stranger', password='strangerpass')

        resp = self.client.get(reverse('memory:memory_edit', args=[self.m1.id]))
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(reverse('memory:memory_delete', args=[self.m1.id]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Memory.objects.filter(id=self.m1.id).exists())


class MemoryListDesktopLayoutTest(TestCase):
    """记忆管理页桌面两列布局回归锁（右列 = 搜索 + 类别筛选）

    本页原来套 max-w-3xl 居中壳，桌面端右侧白掉约 448px。
    rail-first 保证移动端顺序（搜索 → 类别 → 列表）与改造前逐块一致；
    搜索/筛选上的 hx-* 契约必须原样保留（这两个端点返回 HTML 片段，走 HTMX 是对的）。
    """
    TEMPLATE = Path(__file__).resolve().parent.parent / 'templates' / 'memory' / 'memory_list.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='testpass')
        self.client = Client()
        self.client.login(username='raven', password='testpass')
        Memory.objects.create(user=self.user, content='喜欢喝咖啡', category='preference')
        self.html = self.client.get(reverse('memory:memory_list')).content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('id="memory-list"', 'HTMX 局部刷新区'), ('喜欢喝咖啡', '记忆条目')],
            right=[('搜索记忆...', '搜索框'), ('id="memory-category-filter"', '类别筛选区')],
            mobile_order=['搜索记忆...', 'id="memory-list"', '喜欢喝咖啡'],
            rail_first=True)

    def test_htmx_targets_survive_the_split(self):
        """控件搬进右列后 hx-target / hx-include 关系不能断（跨列引用靠 id，仍能命中）"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        self.assertIn('hx-target="#memory-list"', src)
        self.assertIn('hx-include="#memory-category-filter"', src)
        self.assertIn('hx-include="#memory-search"', src)
        self.assertIn('id="memory-search"', src)


class ArchiveSummaryMemoryTest(TestCase):
    """归档对话 → 一条「讨论过什么 + 结论」的记忆（跨会话可回忆）

    刻意做成启发式（取首问 + 末答）而不是再叫一次 AI 总结：归档时 session 刚被
    cancel，再发一轮要等几十秒、可能撞 409，还会在历史里留下一条假的 assistant 消息。
    """

    def setUp(self):
        self.user = User.objects.create_user('t', password='p')
        self.client.force_login(self.user)
        self.conv = Conversation.objects.create(
            user=self.user, session_id='sess_sum', agent_id='ag_sum', title='桐庐行程')

    def _say(self, role, content):
        return Message.objects.create(conversation=self.conv, role=role, content=content)

    def _fill(self, with_answer=True):
        self._say('user', '桐庐周末去哪玩比较好')
        self._say('user', '带上小孩方便吗')
        if with_answer:
            self._say('assistant', '**结论**：去 `龙井峡`，漂流适合 6 岁以上\n\n| 项目 | 价格 |\n| --- | --- |')

    def test_creates_one_summary_memory(self):
        self._fill()
        memory = summarize_conversation_for_memory(self.conv)
        self.assertIsNotNone(memory)
        self.assertEqual(memory.category, 'other')
        self.assertEqual(memory.importance, 4, '索引型记忆不得抢用户偏好（5-8）的注入位')
        self.assertIn('桐庐行程', memory.content)
        self.assertIn('讨论了：桐庐周末去哪玩比较好', memory.content)
        self.assertIn('结论：', memory.content)

    def test_markdown_noise_is_stripped_from_the_excerpt(self):
        """AI 回复本来就是 Markdown：星号/井号/表格竖线进记忆就成了排版噪声

        记忆是给模型读的，下次注入上下文时不该带一串 ** 与 |。
        """
        self._fill()
        memory = summarize_conversation_for_memory(self.conv)
        for noise in ('**', '`', '|', '\n'):
            self.assertNotIn(noise, memory.content)
        self.assertIn('龙井峡', memory.content)
        self.assertIn('结论：', memory.content)   # 清噪声不能把整段结论弄没

    def test_title_that_equals_the_first_question_is_not_repeated(self):
        """建对话时标题就是首条提问的截断，再写一遍「讨论了」等于同一句说两次"""
        self.conv.title = '桐庐周末去哪玩比较好'
        self.conv.save(update_fields=['title'])
        self._fill()
        memory = summarize_conversation_for_memory(self.conv)
        self.assertIn('对话「桐庐周末去哪玩比较好」', memory.content)
        self.assertNotIn('讨论了：', memory.content)
        self.assertIn('结论：', memory.content)

    def test_nothing_new_to_say_returns_none(self):
        """只剩一个光标题（没结论、提问又与标题重复）的记忆没有信息量"""
        self.conv.title = '桐庐周末去哪玩比较好'
        self.conv.save(update_fields=['title'])
        self._say('user', '桐庐周末去哪玩比较好')
        self._say('user', '再想想')
        self.assertIsNone(summarize_conversation_for_memory(self.conv))
        self.assertEqual(Memory.objects.count(), 0)

    def test_skips_single_question_conversations(self):
        self._say('user', '你好')
        self._say('assistant', '你好，有什么可以帮你')
        self.assertIsNone(summarize_conversation_for_memory(self.conv))
        self.assertEqual(Memory.objects.count(), 0)

    def test_skips_when_the_conversation_already_produced_memories(self):
        """协议里 AI 一直在主动记（memory 字段），归档别再重复一层"""
        self._fill()
        answer = self.conv.messages.filter(role='assistant').first()
        Memory.objects.create(user=self.user, content='家里有小孩', category='relationship',
                              importance=7, source_message=answer)
        self.assertIsNone(summarize_conversation_for_memory(self.conv))
        self.assertEqual(Memory.objects.filter(content__startswith='对话「').count(), 0)

    def test_other_users_memories_do_not_block_and_are_not_written(self):
        self._fill()
        other = User.objects.create_user('o', password='p')
        Memory.objects.create(user=other, content='别人的记忆')
        memory = summarize_conversation_for_memory(self.conv)
        self.assertIsNotNone(memory)
        self.assertEqual(memory.user_id, self.user.id)

    def test_long_answers_are_capped(self):
        self._fill(with_answer=False)
        self._say('assistant', '长' * 2000)
        memory = summarize_conversation_for_memory(self.conv)
        self.assertLessEqual(len(memory.content), 500)   # Memory.content 是 max_length=500

    def test_archiving_through_the_view_writes_the_memory(self):
        """整条链路：点「归档对话」就应该沉淀，不依赖调用方记得手动跑一次"""
        self._fill()
        resp = self.client.post(reverse('chat:archive_conversation', args=[self.conv.id]))
        self.assertEqual(resp.status_code, 302)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.status, 'archived')
        self.assertEqual(Memory.objects.filter(content__startswith='对话「').count(), 1)

    def test_archiving_still_works_when_the_summary_blows_up(self):
        """摘要失败只能少一条记忆，不能把归档本身弄挂"""
        self._fill()
        with patch('memory.services.summarize_conversation_for_memory',
                   side_effect=RuntimeError('boom')):
            resp = self.client.post(reverse('chat:archive_conversation', args=[self.conv.id]))
        self.assertEqual(resp.status_code, 302)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.status, 'archived')
