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


class MemoryDecayScoreTest(TestCase):
    """记忆动态权重：importance × 时间衰减 + access_count 加分"""

    def setUp(self):
        self.user = User.objects.create_user('decay_user', password='p')

    def _make(self, content, importance, days_ago, access_count=0):
        """创建一条记忆并手动设置 updated_at 和 access_count"""
        from django.utils import timezone
        from datetime import timedelta
        m = Memory.objects.create(
            user=self.user, content=content,
            category='other', importance=importance,
        )
        # 回拨 updated_at 模拟“N 天前更新”
        past = timezone.now() - timedelta(days=days_ago)
        Memory.objects.filter(id=m.id).update(updated_at=past, access_count=access_count)
        m.refresh_from_db()
        return m

    def test_recent_low_beats_old_high(self):
        """昨天 importance=5 的记忆应排在 90 天前 importance=8 之前

        90 天 ≈ 3 个半衰期，8 × 0.125 = 1.0；5 × ~0.977 ≈ 4.9。
        """
        self._make('旧的高分', importance=8, days_ago=90)
        self._make('新的低分', importance=5, days_ago=1)

        results = retrieve_memories(self.user, limit=2)
        self.assertEqual(results[0].content, '新的低分')
        self.assertEqual(results[1].content, '旧的高分')

    def test_access_count_breaks_tie(self):
        """相同 importance、相同时间的两条记忆，访问多的排前"""
        self._make('少访问', importance=5, days_ago=0, access_count=0)
        self._make('多访问', importance=5, days_ago=0, access_count=15)

        results = retrieve_memories(self.user, limit=2)
        self.assertEqual(results[0].content, '多访问')

    def test_access_bonus_caps_at_2(self):
        """access_count 超过 20 后加分不再增长（防止刷访问抢位）"""
        from memory.services import _memory_score, _ACCESS_BONUS_CAP
        from django.utils import timezone
        m = self._make('高频', importance=5, days_ago=0, access_count=100)
        score = _memory_score(m, timezone.now())
        # importance=5 × decay≈1 + cap=2.0 → 约 7.0
        self.assertAlmostEqual(score, 5.0 + _ACCESS_BONUS_CAP, places=1)

    def test_decay_half_life_is_30_days(self):
        """30 天前的记忆权重应为原始的约一半"""
        from memory.services import _memory_score
        from django.utils import timezone
        m = self._make('半月前', importance=10, days_ago=30, access_count=0)
        score = _memory_score(m, timezone.now())
        # 10 × 0.5^1 = 5.0
        self.assertAlmostEqual(score, 5.0, delta=0.5)


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


class ConsolidatedMemoryTest(TestCase):
    """consolidated 字段：已聚合记忆在检索/去重/工具中被跳过"""

    def setUp(self):
        self.user = User.objects.create_user('consol_user', password='p')
        self.active = Memory.objects.create(
            user=self.user, content='活跃记忆', category='preference', importance=5,
        )
        self.consolidated = Memory.objects.create(
            user=self.user, content='已聚合记忆', category='preference',
            importance=8, consolidated=True,
        )

    def test_retrieve_skips_consolidated(self):
        results = retrieve_memories(self.user, limit=10)
        contents = [m.content for m in results]
        self.assertIn('活跃记忆', contents)
        self.assertNotIn('已聚合记忆', contents)

    def test_similarity_check_skips_consolidated(self):
        """去重检查不应拿已聚合记忆当参照，否则新记忆会被误判为重复"""
        from memory.services import _is_similar_content
        # 已聚合记忆的内容是「已聚合记忆」，如果它参与去重，
        # 创建一条内容相近的新记忆会被跳过
        self.assertFalse(_is_similar_content(self.user, '已聚合记忆的新版本'))

    def test_memory_search_tool_skips_consolidated(self):
        from core.agent_registry import get_tool
        tool = get_tool('memory.search')
        result = tool['fn'](self.user, {'query': '聚合'})
        self.assertIn('没有找到', result['reply'])


class ConsolidateCommandTest(TestCase):
    """consolidate_memories 管理命令：dry-run + 分组逻辑"""

    def setUp(self):
        self.user = User.objects.create_user('cmd_user', password='p')

    def test_dry_run_does_not_modify_data(self):
        """dry-run 只打印，不改数据库"""
        for i in range(6):
            Memory.objects.create(
                user=self.user, content=f'偏好{i}', category='preference', importance=5,
            )
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('consolidate_memories', '--dry-run', stdout=out)
        output = out.getvalue()
        self.assertIn('DRY-RUN', output)
        # 没有任何记忆被标记为 consolidated
        self.assertEqual(Memory.objects.filter(consolidated=True).count(), 0)

    def test_no_groups_below_min_size(self):
        """不足 5 条的组不触发"""
        for i in range(3):
            Memory.objects.create(
                user=self.user, content=f'事实{i}', category='fact', importance=5,
            )
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('consolidate_memories', '--dry-run', stdout=out)
        self.assertIn('没有需要聚合', out.getvalue())


# 把被挤出的方法手动绑回 ArchiveSummaryMemoryTest
import types as _types

def _nothing_new(self):
    """只剩一个光标题（没结论、提问又与标题重复）的记忆没有信息量"""
    self.conv.title = '桐庐周末去哪玩比较好'
    self.conv.save(update_fields=['title'])
    self._say('user', '桐庐周末去哪玩比较好')
    self._say('user', '再想想')
    self.assertIsNone(summarize_conversation_for_memory(self.conv))
    self.assertEqual(Memory.objects.count(), 0)

def _skips_single(self):
    self._say('user', '你好')
    self._say('assistant', '你好，有什么可以帮你')
    self.assertIsNone(summarize_conversation_for_memory(self.conv))
    self.assertEqual(Memory.objects.count(), 0)

def _skips_existing(self):
    """协议里 AI 一直在主动记（memory 字段），归档别再重复一层"""
    self._fill()
    answer = self.conv.messages.filter(role='assistant').first()
    Memory.objects.create(user=self.user, content='家里有小孩', category='relationship',
                          importance=7, source_message=answer)
    self.assertIsNone(summarize_conversation_for_memory(self.conv))
    self.assertEqual(Memory.objects.filter(content__startswith='对话「').count(), 0)

def _other_users(self):
    self._fill()
    other = User.objects.create_user('o', password='p')
    Memory.objects.create(user=other, content='别人的记忆')
    memory = summarize_conversation_for_memory(self.conv)
    self.assertIsNotNone(memory)
    self.assertEqual(memory.user_id, self.user.id)

def _long_answers(self):
    self._fill(with_answer=False)
    self._say('assistant', '长' * 2000)
    memory = summarize_conversation_for_memory(self.conv)
    self.assertLessEqual(len(memory.content), 500)

def _archive_view(self):
    """整条链路：点「归档对话」就应该沉淀，不依赖调用方记得手动跑一次"""
    self._fill()
    resp = self.client.post(reverse('chat:archive_conversation', args=[self.conv.id]))
    self.assertEqual(resp.status_code, 302)
    self.conv.refresh_from_db()
    self.assertEqual(self.conv.status, 'archived')
    self.assertEqual(Memory.objects.filter(content__startswith='对话「').count(), 1)

def _archive_blows_up(self):
    """摘要失败只能少一条记忆，不能把归档本身弄挂"""
    self._fill()
    with patch('memory.services.summarize_conversation_for_memory',
               side_effect=RuntimeError('boom')):
        resp = self.client.post(reverse('chat:archive_conversation', args=[self.conv.id]))
    self.assertEqual(resp.status_code, 302)
    self.conv.refresh_from_db()
    self.assertEqual(self.conv.status, 'archived')

# 绑定回原类
for _name, _fn in [
    ('test_nothing_new_to_say_returns_none', _nothing_new),
    ('test_skips_single_question_conversations', _skips_single),
    ('test_skips_when_the_conversation_already_produced_memories', _skips_existing),
    ('test_other_users_memories_do_not_block_and_are_not_written', _other_users),
    ('test_long_answers_are_capped', _long_answers),
    ('test_archiving_through_the_view_writes_the_memory', _archive_view),
    ('test_archiving_still_works_when_the_summary_blows_up', _archive_blows_up),
]:
    setattr(ArchiveSummaryMemoryTest, _name, _fn)

# 删除被挤出的残余方法（它们在 ConsolidateCommandTest 里是无效的）
for _attr in list(ConsolidateCommandTest.__dict__.keys()):
    if _attr.startswith('test_') and _attr not in (
        'test_dry_run_does_not_modify_data',
        'test_no_groups_below_min_size',
    ):
        delattr(ConsolidateCommandTest, _attr)
