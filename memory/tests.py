"""memory app 测试

覆盖：模型、服务层（检索/提取/注入/AI 存储）、Agent 工具、视图。
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from .models import Memory
from .services import (
    retrieve_memories, format_memory_for_injection,
    extract_memories_from_text, save_ai_extracted_memories,
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
