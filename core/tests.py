import json
import re
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import (TestCase, Client, RequestFactory, SimpleTestCase,
                     override_settings)
from django.urls import reverse

from core.tags import (add_tags, apply_tags, active_tag_names, split_tag_names,
                       tag_names, tag_suggestions, used_tags)
from core.models import Tag
from activities.models import Activity, Expense
from knowledge.models import Article
from notes.models import Note
from chat.models import Conversation, Message
from django.http import Http404
from core.utils import (visible_child_qs, get_visible_child, q_or,
                        char_overlap_ratio)
from core.cross_link import get_related_content, _tag_intersection_scores
from core.search import global_search
from datetime import timedelta
from django.utils import timezone
from core.layout_asserts import (assert_desktop_two_columns, code_only,
                                python_code_only)
from core.report_generator import (collect_report_data, generate_report,
                                   save_report_to_knowledge, _fallback_report)


class CrossLinkTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        # 创建带标签的活动
        self.activity = Activity.objects.create(user=self.user, name='桐庐旅行')
        apply_tags(self.activity, ['旅行', '周末'])

        # 创建带共同标签的知识库文章
        self.article1 = Article.objects.create(user=self.user, title='桐庐攻略', content='详细攻略...')
        apply_tags(self.article1, ['旅行', '桐庐'])

        self.article2 = Article.objects.create(user=self.user, title='杭州周边游', content='推荐...')
        apply_tags(self.article2, ['旅行'])

        # 创建带共同标签的笔记
        self.note1 = Note.objects.create(user=self.user, content='周末去桐庐玩，记得带泳衣')
        apply_tags(self.note1, ['旅行', '周末'])

    def test_tag_intersection_basic(self):
        """标签交集计算正确"""
        results = _tag_intersection_scores(
            ['旅行', '周末'], Article,
            Article.objects.filter(user=self.user), limit=5
        )
        self.assertTrue(len(results) >= 1)
        # article1 有 1 个共同标签（旅行），note1 有 2 个（旅行+周末）
        # article1 和 article2 都只有 1 个共同标签（旅行）
        self.assertEqual(results[0]['score'], 1)

    def test_tag_intersection_empty_source(self):
        """源无标签返回空"""
        results = _tag_intersection_scores(
            [], Article,
            Article.objects.filter(user=self.user), limit=5
        )
        self.assertEqual(results, [])

    def test_get_related_content_from_activity(self):
        """从 Activity 调用，返回 articles 和 notes（不含 activities）"""
        related = get_related_content(self.user, Activity, self.activity, limit=5)
        self.assertIn('articles', related)
        self.assertIn('notes', related)
        self.assertNotIn('activities', related)
        # article1 应该被推荐（共同标签：旅行）
        article_ids = [r['object'].id for r in related['articles']]
        self.assertIn(self.article1.id, article_ids)

    def test_get_related_content_from_article(self):
        """从 Article 调用，返回 activities 和 notes（不含 articles）"""
        related = get_related_content(self.user, Article, self.article1, limit=5)
        self.assertIn('activities', related)
        self.assertIn('notes', related)
        self.assertNotIn('articles', related)
        # activity 应该被推荐（共同标签：旅行）
        activity_ids = [r['object'].id for r in related['activities']]
        self.assertIn(self.activity.id, activity_ids)

    def test_get_related_content_from_note(self):
        """从 Note 调用，返回 activities 和 articles（不含 notes）"""
        related = get_related_content(self.user, Note, self.note1, limit=5)
        self.assertIn('activities', related)
        self.assertIn('articles', related)
        self.assertNotIn('notes', related)

    def test_token_fallback(self):
        """标签不足时分词兜底"""
        # 创建一个没有标签但标题包含关键词的文章
        article3 = Article.objects.create(
            user=self.user, title='桐庐龙井峡漂流', content='刺激的漂流体验'
        )
        # 使用一个名称分词后能独立匹配 article3 的活动
        # "桐庐" 分词为 ["桐庐"]，article3 标题含 "桐庐" → icontains 匹配
        activity2 = Activity.objects.create(user=self.user, name='桐庐')
        apply_tags(activity2, ['户外'])  # 与 article3 无标签交集
        related = get_related_content(self.user, Activity, activity2, limit=10)
        article_ids = [r['object'].id for r in related['articles']]
        self.assertIn(article3.id, article_ids)

    def test_related_content_caching(self):
        """推荐结果被缓存（两次调用结果一致）"""
        related1 = get_related_content(self.user, Activity, self.activity, limit=5)
        related2 = get_related_content(self.user, Activity, self.activity, limit=5)
        self.assertEqual(len(related1['articles']), len(related2['articles']))

    def test_exclude_self(self):
        """推荐结果不包含源实例自身（同模型类型时）"""
        # 创建两个互相有共同标签的活动
        activity2 = Activity.objects.create(user=self.user, name='杭州周末游')
        apply_tags(activity2, ['旅行', '周末'])
        related = get_related_content(self.user, Activity, activity2, limit=5)
        # 结果中不应包含 activity2 自身（Activity 结果在 'articles'/'notes' 里，不含 'activities'）
        self.assertNotIn('activities', related)

    def test_score_reflects_shared_tags(self):
        """分数反映共同标签数量"""
        # note1 有 '旅行'+'周末' 两个标签与 activity 相同
        related = get_related_content(self.user, Activity, self.activity, limit=5)
        note_results = related.get('notes', [])
        note1_result = next((r for r in note_results if r['object'].id == self.note1.id), None)
        self.assertIsNotNone(note1_result)
        self.assertEqual(note1_result['score'], 2)  # 旅行 + 周末


class GlobalSearchTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()

        # 创建测试数据
        self.activity = Activity.objects.create(user=self.user, name='桐庐旅行计划')
        apply_tags(self.activity, ['旅行'])

        self.article = Article.objects.create(user=self.user, title='桐庐攻略', content='详细攻略内容')
        self.note = Note.objects.create(user=self.user, content='周末去桐庐玩')

    def test_global_search_activities(self):
        results = global_search(self.user, '桐庐')
        self.assertTrue(any(a.id == self.activity.id for a in results['activities']))

    def test_global_search_articles(self):
        results = global_search(self.user, '攻略')
        self.assertTrue(any(a.id == self.article.id for a in results['articles']))

    def test_global_search_notes(self):
        results = global_search(self.user, '周末')
        self.assertTrue(any(n.id == self.note.id for n in results['notes']))

    def test_global_search_empty_query(self):
        results = global_search(self.user, '')
        self.assertEqual(sum(len(v) for v in results.values()), 0)

    def test_global_search_no_results(self):
        results = global_search(self.user, '不存在的关键词xyz')
        self.assertEqual(sum(len(v) for v in results.values()), 0)

    def test_global_search_limit(self):
        """每模块最多 limit_per_module 条"""
        for i in range(10):
            Activity.objects.create(user=self.user, name=f'测试活动{i}')
        results = global_search(self.user, '测试', limit_per_module=5)
        self.assertLessEqual(len(results['activities']), 5)

    def test_search_api_auth(self):
        """未登录返回 302"""
        response = self.client.get('/api/search/', {'q': 'test'})
        self.assertEqual(response.status_code, 302)

    def test_search_api_authenticated(self):
        """登录后正常返回"""
        self.client.login(username='testuser', password='test')
        response = self.client.get('/api/search/', {'q': '桐庐'})
        self.assertEqual(response.status_code, 200)


class PythonCodeOnlyTest(SimpleTestCase):
    """python_code_only 自身的回归锁（上面那条静态锁的地基）

    为什么单独锁：它坏掉的方式恰好是静默的 —— 少剔一段 docstring 只会让某条锁
    假失败（有人为了过锁去改注释），多剔一行代码则会让锁空跑（该报的不报）。
    集成测试分不开这两种情况，只能直接喂样本。
    """

    SRC = (
        '"""模块说明：旧写法是 Reminder.objects.filter(status=\'pending\')。"""\n'
        'def f(user):\n'
        '    """函数说明：别学它写 status=\'fired\'。"""\n'
        "    # 注释里的 status='dismissed' 也不算代码\n"
        "    color = '#fff'  # 引号里的 # 不是注释，这行得整行留下\n"
        "    return Model.objects.filter(user=user, status='ready')\n"
    )

    def test_docstrings_and_comments_are_stripped(self):
        code = python_code_only(self.SRC)
        for gone in ("status='pending'", "status='fired'", "status='dismissed'"):
            self.assertNotIn(gone, code, '说明文字没被剔掉：静态锁会因自己的注释假失败')

    def test_real_code_survives(self):
        code = python_code_only(self.SRC)
        self.assertIn("status='ready'", code, '真代码被剔掉了 —— 锁会空跑')
        self.assertIn("color = '#fff'", code,
                      '引号里的 # 被当成注释，整行赋值会连同后面的代码一起丢')

    def test_line_numbers_are_preserved(self):
        # 剔行不拆行：否则用 findall 拿到的行号与源码对不上，报错信息会指向错误位置
        self.assertEqual(len(python_code_only(self.SRC).splitlines()),
                         len(self.SRC.splitlines()))


class ReportGeneratorTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        # 创建测试活动和费用
        self.activity1 = Activity.objects.create(
            user=self.user, name='出差上海', status='done',
            start_date=timezone.localdate(),
        )
        expense = Expense.objects.create(
            activity=self.activity1, user=self.user,
            amount=500, paid_at=timezone.localdate(),
        )
        apply_tags(expense, ['交通'])

    def test_collect_report_data_weekly(self):
        """周数据聚合正确"""
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        data = collect_report_data(self.user, 'weekly', week_start, today)
        self.assertEqual(data['total_activities'], 1)
        self.assertEqual(data['total_expense'], 500.0)
        self.assertIn('交通', data['expense_by_tag'])

    def test_collect_report_data_monthly(self):
        """月数据聚合正确"""
        today = timezone.localdate()
        month_start = today.replace(day=1)
        data = collect_report_data(self.user, 'monthly', month_start, today)
        self.assertGreaterEqual(data['total_activities'], 1)

    def test_fallback_report(self):
        """AI 失败降级为纯数据模板"""
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        data = collect_report_data(self.user, 'weekly', week_start, today)
        markdown = _fallback_report(data, 'weekly', week_start, today)
        self.assertIn('周报', markdown)
        self.assertIn('¥500', markdown)

    def test_generate_report_with_fallback(self):
        """generate_report 在 AI 不可用时降级"""
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        markdown, data = generate_report(self.user, 'weekly', week_start, today)
        # AI 可能不可用（测试环境），但应该有输出
        self.assertTrue(len(markdown) > 0)
        self.assertIsNotNone(data)

    def test_save_report_to_knowledge(self):
        """报告保存为 Article + 标签"""
        article = save_report_to_knowledge(
            self.user, 'weekly', '测试周报', '# 测试内容\n\n这是一份测试报告。'
        )
        self.assertEqual(article.title, '测试周报')
        self.assertTrue(article.tags.filter(name='report-weekly').exists())


class ReportViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def test_weekly_report_view(self):
        """周报视图返回 200"""
        response = self.client.get('/reports/weekly/')
        self.assertEqual(response.status_code, 200)

    def test_monthly_report_view(self):
        """月报视图返回 200"""
        response = self.client.get('/reports/monthly/')
        self.assertEqual(response.status_code, 200)


class ReportAgentToolTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')

    def test_report_tool_exists(self):
        from core.agent_registry import get_tool
        tool = get_tool('reports.generate')
        self.assertIsNotNone(tool)

    def test_report_tool_invalid_type(self):
        from core.agent_registry import get_tool, ToolError
        tool = get_tool('reports.generate')
        with self.assertRaises(ToolError):
            tool['fn'](self.user, {'report_type': 'invalid'})

    def test_report_tool_weekly(self):
        from core.agent_registry import get_tool
        tool = get_tool('reports.generate')
        result = tool['fn'](self.user, {'report_type': 'weekly'})
        self.assertIn('周报', result['reply'])
        self.assertEqual(result['card'], 'report')


class VisibilityHelperTest(TestCase):
    """可见性工具函数（H4/M8 收敛后的唯一口径）

    规则：浏览/管理/搜索类走 visible_qs（超管见全部）；
    子模型（自身无 user 字段）走 visible_child_qs，不能再手写 __user= 分支。
    """

    def setUp(self):
        self.owner = User.objects.create_user('owner', password='p')
        self.other = User.objects.create_user('other', password='p')
        self.conv = Conversation.objects.create(
            user=self.owner, session_id='sess_owner', agent_id='agent_1', title='标题')
        self.msg = Message.objects.create(
            conversation=self.conv, role='user', content='会话里的关键词内容')

    def test_visible_child_qs_scopes_by_parent(self):
        self.assertEqual(
            list(visible_child_qs(Message, self.owner, 'conversation')), [self.msg])
        self.assertEqual(
            list(visible_child_qs(Message, self.other, 'conversation')), [])

    def test_superuser_sees_child_of_other_user(self):
        admin = User.objects.create_superuser('root', password='p')
        self.assertEqual(
            list(visible_child_qs(Message, admin, 'conversation')), [self.msg])

    def test_get_visible_child_404_for_non_owner(self):
        with self.assertRaises(Http404):
            get_visible_child(Message, self.other, 'conversation', id=self.msg.id)
        self.assertEqual(
            get_visible_child(Message, self.owner, 'conversation', id=self.msg.id), self.msg)

    def test_q_or_matches_any_field(self):
        a = Activity.objects.create(user=self.owner, name='骑行计划')
        apply_tags(a, ['运动'])
        Activity.objects.create(user=self.owner, name='工作总结')
        qs = Activity.objects.filter(q_or(('name', 'tags__name'), '运动'))
        self.assertEqual(list(qs), [a])

    def test_char_overlap_ratio_two_modes_differ(self):
        """单向覆盖率 vs 双向相似度：a 短 b 长时两者不同，因此保留两个 mode 而不是强行合并"""
        self.assertEqual(char_overlap_ratio('ab', 'abc', mode='contains'), 1.0)
        self.assertAlmostEqual(char_overlap_ratio('ab', 'abc'), 2 / 3)
        # contains 逐位计数：重复字符各自计入（'aab' 三个字符都出现在 'ab' 里）
        self.assertEqual(char_overlap_ratio('aab', 'ab', mode='contains'), 1.0)
        self.assertEqual(char_overlap_ratio('', 'abc'), 0.0)


class GlobalSearchVisibilityTest(TestCase):
    """全局搜索各模块必须同一可见性口径（H4 的实证：笔记/消息曾落后于其他模块）"""

    def setUp(self):
        self.owner = User.objects.create_user('owner', password='p')
        self.note = Note.objects.create(user=self.owner, content='他人的笔记关键词')
        self.conv = Conversation.objects.create(
            user=self.owner, session_id='sess_other_note', agent_id='agent_1')
        Message.objects.create(conversation=self.conv, role='user',
                               content='他人的消息关键词')

    def test_superuser_finds_other_users_notes_and_messages(self):
        admin = User.objects.create_superuser('root', password='p')
        results = global_search(admin, '关键词')
        self.assertIn(self.note.id, [n.id for n in results['notes']])
        self.assertEqual(len(results['messages']), 1)

    def test_regular_user_still_isolated(self):
        stranger = User.objects.create_user('stranger', password='p')
        results = global_search(stranger, '关键词')
        self.assertEqual(sum(len(v) for v in results.values()), 0)


class CrossAppVisibilityTest(TestCase):
    """H4：笔记 / 文章页的可见性必须与活动模块同一口径（均经 core/utils）

    以前这两个 app 是“仅本人可见”，活动是“超管全可见”，同一个超管在不同
    页面上看到的数据范围不一致。
    """

    def setUp(self):
        self.owner = User.objects.create_user('owner', password='p')
        self.note = Note.objects.create(user=self.owner, content='超管可见的笔记')
        self.article = Article.objects.create(
            user=self.owner, title='超管可见的文章', content='正文')
        self.client = Client()
        self.admin = User.objects.create_superuser('root', password='p')
        self.stranger = User.objects.create_user('stranger', password='p')

    def test_superuser_lists_other_users_content(self):
        self.client.login(username='root', password='p')
        self.assertContains(self.client.get(reverse('notes:note_list')), '超管可见的笔记')
        self.assertContains(self.client.get(reverse('knowledge:article_list')), '超管可见的文章')

    def test_stranger_cannot_reach_single_objects(self):
        self.client.login(username='stranger', password='p')
        self.assertEqual(
            self.client.get(reverse('notes:note_edit', args=[self.note.id])).status_code, 404)
        self.assertEqual(
            self.client.get(reverse('knowledge:article_edit', args=[self.article.pk])).status_code, 404)
        self.assertEqual(
            self.client.get(reverse('knowledge:article_detail', args=[self.article.slug])).status_code, 404)

    def test_superuser_can_open_other_users_objects(self):
        self.client.login(username='root', password='p')
        self.assertEqual(
            self.client.get(reverse('notes:note_edit', args=[self.note.id])).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('knowledge:article_detail', args=[self.article.slug])).status_code, 200)


class AgentRegistryConsistencyTest(TestCase):
    """注册表自洽性回归锁

    起因：意图协议是根据 INTENT_TOOL_MAP + 注册表动态生成的，如果某个意图绑定了
    未注册的工具（改名/漏写 decorator/模块没被自动发现），协议会静默跳过它 ——
    线上表现只是「AI 不会做这件事」，且不报任何错，极难定位。
    """

    def test_every_intent_maps_to_registered_tool(self):
        from core.agent_registry import INTENT_TOOL_MAP, get_tool
        missing = [(intent, name) for intent, name in INTENT_TOOL_MAP.items()
                   if get_tool(name) is None]
        self.assertEqual(missing, [], f'这些意图绑定了未注册的工具：{missing}')

    def test_every_registered_tool_is_reachable_from_chat(self):
        """未绑意图的工具在对话里永远调不到；确实要只给内部用的，改这条断言并注明理由"""
        from core.agent_registry import INTENT_TOOL_MAP, _REGISTRY
        bound = set(INTENT_TOOL_MAP.values())
        self.assertEqual(sorted(set(_REGISTRY) - bound), [])

    def test_prompt_advertises_conversation_to_store_capabilities(self):
        """对话结论落库的两个出口都要在协议里可见：存知识库、写活动描述"""
        from core.agent_registry import build_protocol_prompt
        prompt = build_protocol_prompt()
        self.assertIn('knowledge_create', prompt)
        self.assertIn('存成一篇知识库文章', prompt)
        self.assertIn('默认追加到原描述末尾', prompt)


class SiteBrandTest(TestCase):
    """站点品牌名统一口径回归锁

    品牌名以前在导航、各页标题、登录页、Admin、PWA manifest、AI 自我介绍里各写各的
    字面量（AI Assistant / Personal AI Assistant / 个人助手 三套并存），改一次名要扫
    全站。现在唯一口径是 settings.SITE_NAME，模板里一律用 {{ SITE_NAME }}。

    manifest.json 是唯一没法用模板变量的地方（静态文件不经 Django），改名最容易漏，
    所以单独断言它与 settings 一致。
    """

    OLD_NAMES = ('Personal AI Assistant', 'AI Assistant', '个人助手')

    def _html_templates(self):
        return sorted((Path(settings.BASE_DIR) / 'templates').rglob('*.html'))

    def test_no_template_hardcodes_brand(self):
        offenders = []
        for tpl in self._html_templates():
            text = tpl.read_text(encoding='utf-8')
            for name in self.OLD_NAMES:
                if name in text:
                    offenders.append(f'{tpl.name}: {name}')
        self.assertEqual(offenders, [], f'模板里又出现写死的品牌名：{offenders}')

    def test_every_page_title_carries_brand(self):
        """继承 base 的页面标题必须带品牌，否则浏览器标签页上认不出是哪个站"""
        pattern = re.compile(r'{%\s*block title\s*%(.*?)\{%\s*endblock\s*%', re.S)
        missing = []
        for tpl in self._html_templates():
            text = tpl.read_text(encoding='utf-8')
            if 'extends "base.html"' not in text:
                continue
            for body in pattern.findall(text):
                if 'SITE_NAME' not in body:
                    missing.append(tpl.name)
        self.assertEqual(missing, [], f'这些页面标题没带品牌名：{missing}')

    def test_manifest_matches_site_name(self):
        path = Path(settings.BASE_DIR) / 'static' / 'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['name'], settings.SITE_NAME,
                         'PWA 安装名是静态文件，改名时要手动同步')
        self.assertEqual(manifest['short_name'], settings.SITE_NAME)

    def test_admin_and_ai_prompt_use_brand(self):
        from django.contrib import admin
        from core.agent_registry import build_protocol_prompt
        self.assertIn(settings.SITE_NAME, admin.site.site_header)
        self.assertIn(f'「{settings.SITE_NAME}」', build_protocol_prompt())

    @override_settings(SITE_NAME='TESTBRAND')
    def test_brand_comes_from_settings_not_literal(self):
        """改 settings 就能改名：页面标题、导航标 alt、登录页品牌标 alt 都要跟着变"""
        resp = self.client.get(reverse('login'))
        self.assertContains(resp, 'TESTBRAND')
        for name in self.OLD_NAMES:
            self.assertNotContains(resp, name)


class BrandAssetTest(TestCase):
    """品牌图资源一致性回归锁

    以前 manifest 只挂了一个 icon.svg（内容是「AI」字样的旧占位图），改名换标后
    没人发现它已经不对。这里把「模板引用的图标路径」和「manifest 声明的图标」都
    对到磁盘上的真实文件，并保证登录页/导航用的是新资源。
    图本身由 brand/make_logo_assets.py 从设计源图生成，不手写。
    """

    STATIC = Path(settings.BASE_DIR) / 'static'

    def _manifest(self):
        return json.loads((self.STATIC / 'manifest.json').read_text(encoding='utf-8'))

    def test_manifest_icons_point_at_existing_files(self):
        icons = self._manifest()['icons']
        self.assertTrue(icons, 'manifest 必须声明图标')
        for icon in icons:
            rel = icon['src'].replace('/static/', '')
            path = self.STATIC / rel
            self.assertTrue(path.is_file(), f'manifest 图标不存在：{icon["src"]}')
            # 空文件 / LFS 指针 / 误存成文本都会在这里被拦下
            self.assertTrue(path.read_bytes().startswith(b'\x89PNG'), f'{rel} 不是 PNG')
        purposes = ' '.join(i.get('purpose', '') for i in icons)
        self.assertIn('maskable', purposes, '缺 maskable 图标，Android 安装会被系统任意裁切')
        self.assertIn('any', purposes, '缺 purpose=any 图标')

    def test_base_template_links_all_icon_sizes(self):
        html = (Path(settings.BASE_DIR) / 'templates' / 'base.html').read_text(encoding='utf-8')
        referenced = re.findall(r"\{%\s*static '([^']+)'\s*%\}", html)
        for name in ('icons/favicon-16.png', 'icons/favicon-32.png', 'icons/favicon-48.png',
                     'icons/apple-touch-icon.png', 'img/logo-mark.png'):
            self.assertIn(name, referenced, f'base.html 没引用 {name}')
            self.assertTrue((self.STATIC / name).is_file(), f'{name} 资源缺失，重跑 brand/make_logo_assets.py')
        self.assertNotIn('icon.svg', html, '旧的「AI」占位图标已删除，不得再被引用')

    def test_nav_and_login_use_logo_images(self):
        """导航用图形标、登录页用完整标，两处 alt 都是站名（无图/读屏兜底）"""
        base = (Path(settings.BASE_DIR) / 'templates' / 'base.html').read_text(encoding='utf-8')
        mark_tags = [t for t in re.findall(r'<img[^>]*>', base, re.S) if 'logo-mark.png' in t]
        self.assertTrue(mark_tags, '导航没有图形标 <img>')
        self.assertIn('alt="{{ SITE_NAME }}"', mark_tags[0], '导航图形标 alt 必须是站名')
        login = (Path(settings.BASE_DIR) / 'templates' / 'registration' / 'login.html').read_text(encoding='utf-8')
        lockup_tags = [t for t in re.findall(r'<img[^>]*>', login, re.S) if 'logo-lockup.png' in t]
        self.assertTrue(lockup_tags, '登录页没有完整标 <img>')
        self.assertIn('alt="{{ SITE_NAME }}"', lockup_tags[0])
        self.assertTrue((self.STATIC / 'img' / 'logo-lockup.png').is_file())

    def test_manifest_scope_covers_the_whole_site(self):
        """manifest 挂在 /static/ 下，不显式写 scope 时默认作用域就是 /static/，
        而 start_url 是 / —— 两者不一致时 Chrome 直接判「start_url 不在作用域内」，
        站点根本装不上 PWA。跟 Service Worker 的作用域是同一类坑。"""
        manifest = self._manifest()
        self.assertEqual(manifest.get('scope'), '/', "manifest 必须显式声明 scope: '/'")
        self.assertTrue(manifest['start_url'].startswith(manifest['scope']),
                        'start_url 必须落在 scope 内，否则安装会被浏览器拦下')

    def test_rendered_login_page_serves_brand_assets(self):
        resp = self.client.get(reverse('login'))
        # 不断言前导斜杠：那取决于 STATIC_URL 写法，这里要验的是「资源真被渲染进去了」
        for asset in ('img/logo-lockup.png', 'img/logo-mark.png',
                      'icons/favicon-32.png', 'icons/apple-touch-icon.png'):
            self.assertContains(resp, asset, msg_prefix=f'登录页没引用 {asset}')


class PrimaryNavTest(TestCase):
    """主导航条目回归锁（一级模块口径）

    为什么锁：2026-09 把「模板管理」「循环活动」从顶栏与底部 Tab 栏撤掉，收进
    「活动记录」页头部的按钮组。这类瘦身没有任何机制守住 —— 下次加模块时很容易
    顺手往导航里再塞一项（回到臃肿），或者删了条目却忘了改 `grid-cols-N`
    （移动端底栏右边留一格空白，Tailwind 还会把多出的项挤到第二行）。
    底栏列数 == 条目数是这条锁的核心，条目清单只是把决策写下来。
    """

    TEMPLATES = Path(settings.BASE_DIR) / 'templates'

    # 桌面顶栏：五个一级模块，顺序即视觉顺序（「今日」/「工作台」随 /daily/ 与
    # /today/ 页下线移除，信息走对话里的 daily/工作台快捷卡）
    DESKTOP = ['活动记录', 'AI 对话', '备忘', '知识库', '记忆']
    # 移动底栏：比顶栏少「记忆」（用户定的口径 —— 手机上不读记忆，使用频率也不占位）；
    # 「今日」同样随页下线移除，原位置换成快记 dock（button，不算导航条目）
    MOBILE = ['活动', '对话', '备忘', '知识']
    # 已合并进「活动记录」的入口已随功能下线整体移除（2026-09 模板/循环功能删除）

    def _base(self):
        return (self.TEMPLATES / 'base.html').read_text(encoding='utf-8')

    def _desktop_block(self):
        src = self._base()
        start = src.index('<div class="hidden md:ml-10 md:flex md:space-x-2">')
        return src[start:src.index('</div>', start)]

    def _mobile_block(self):
        src = self._base()
        start = src.index('<nav class="md:hidden fixed bottom-0')
        return src[start:src.index('</nav>', start)]

    @staticmethod
    def _labels(block):
        """取每个导航项的可见文案（桌面直接写在 <a> 里，移动端胶囊栏在 <span> 里）"""
        out = []
        for body in re.findall(r'<a\b[^>]*>(.*?)</a>', block, re.S):
            # 胶囊栏（2026-09-13）当前项的 span 才可见，其余带 hidden —— 这里只锁清单不变
            span = re.search(r'<span class="text-xs font-medium[^"]*">([^<]+)</span>', body)
            if span:
                out.append(span.group(1).strip())
            else:
                # 跳过 svg 里的 path 等噪声，取第一行非空文本；没文本时给空串，
                # 让差异以「清单少一项」的断言失败呈现，而不是 StopIteration
                out.append(next((line.strip() for line in body.splitlines() if line.strip()), ''))
        return out

    def test_desktop_nav_lists_exactly_the_first_level_modules(self):
        self.assertEqual(self._labels(self._desktop_block()), self.DESKTOP,
                         '顶栏条目变了：若是有意调整，同步这里的清单与下方注释')

    def test_mobile_tab_lists_exactly_the_first_level_modules(self):
        self.assertEqual(self._labels(self._mobile_block()), self.MOBILE)

    def test_mobile_tab_items_match_the_module_count(self):
        """胶囊栏（2026-09-13 改自 grid 栏）条目数必须等于清单数 ——
        flex 布局下多余/缺失的 <a> 会把胶囊撑歪或留空"""
        block = self._mobile_block()
        items = re.findall(r'<a\b', block)
        self.assertEqual(len(items), len(self._labels(block)),
                         '底栏 <a> 数与条目清单不一致：会有空位或孤立图标')


class TodayPageRetiredTest(TestCase):
    """/today/ 工作台页下线（2026-09-19，与 /daily/ 同口径）：路由保留但重定向回首页，
    旧书签 / 模板 url 标签不破；渲染层已删除，任何残留在 template 里都会 TemplateDoesNotExist"""

    def setUp(self):
        self.user = User.objects.create_user('tb', password='p')
        self.client = Client()
        self.client.login(username='tb', password='p')

    def test_today_url_redirects_home(self):
        r = self.client.get(reverse('today'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('home'))

    def test_template_file_is_gone(self):
        self.assertFalse(
            (Path(settings.BASE_DIR) / 'templates' / 'core' / 'today.html').exists())

    def test_gather_and_payload_still_work(self):
        """页面下线不等于数据层下线：工作台卡仍要靠 gather_today/today_brief_payload"""
        from activities.models import Activity as Act
        from notes.models import Note as N
        today = timezone.localdate()
        Act.objects.create(user=self.user, name='今日待办', start_date=today, status='planned')
        N.objects.create(user=self.user, content='今天要买年糕')
        from core.views import gather_today, today_brief_payload
        data = gather_today(self.user)
        self.assertEqual([a.name for a in data['today_activities']], ['今日待办'])
        self.assertEqual(len(data['recent_notes']), 1)
        payload = today_brief_payload(self.user)
        self.assertEqual(payload['today_activities'][0]['name'], '今日待办')
        self.assertTrue(payload['recent_notes'][0]['ago'].endswith('前'))
        self.assertEqual(payload['focus_items'][0]['name'], '今日待办')


class ServiceWorkerTest(TestCase):
    """Service Worker 根作用域与缓存策略回归锁

    「发布后样式滞后一次」要同时守住两个坑，修一个不够：
      1. 脚本必须从站点根 /sw.js 下发 —— SW 可控作用域上限就是它脚本 URL 的目录，
         挂在 /static/ 下就管不到任何页面（离线降级分支会变成死代码）。
      2. 脚本自己绝不能被缓存 —— 否则浏览器每次更新检查拿回的都是旧脚本，
         升 CACHE_VERSION 也不会生效（旧版就是被自家的静态资源 cache-first 分支坑在这里）。

    另外作用域扩到根之后，这个文件会拦到全站请求，所以 GET-only / 只缓存顶层导航
    这两条也得跟着锁住：不然表单 POST（mode 也是 navigate）会被允许重放，
    HTMX 片段（Accept: text/html,*/*）会被当成页面缓存掉。
    """

    def _sw_source(self):
        return (Path(settings.BASE_DIR) / 'static' / 'sw.js').read_text(encoding='utf-8')

    def _fetch_handler(self):
        """只取 fetch 监听器那一段：PRECACHE_URLS 里也有 /static/css/，在全文里找会拿错位置"""
        src = self._sw_source()
        return src[src.index("addEventListener('fetch'"):]

    def _branch(self, marker):
        """截出某个 `if (…marker…) { … }` 分支本体，到它的 return; 为止。
        不截的话固定窗口会溢到下一个分支，把「本分支用了 networkFirst」误判成「全文都用了」。"""
        handler = self._fetch_handler()
        start = handler.index(marker)
        body = handler[start:]
        return body[:body.index('return;') + len('return;')]

    def test_sw_served_from_site_root_and_public(self):
        """登录页也要能装 SW，这个路由不能挂 @login_required"""
        self.assertEqual(reverse('service_worker'), '/sw.js')
        resp = self.client.get('/sw.js')
        self.assertEqual(resp.status_code, 200, '未登录也被 200，不是被踢去登录页')
        self.assertIn('javascript', resp['Content-Type'])

    def test_sw_answers_head_for_deploy_checks(self):
        """发布后靠 `curl -I /sw.js` 验响应头，HEAD 必须是 200（require_GET 会拒成 405）；
        同时方法限制不能形同虚设，POST 得被拦下"""
        head = self.client.head('/sw.js')
        self.assertEqual(head.status_code, 200, 'HEAD 被拒说明用错了 require_GET，应该是 require_safe')
        self.assertIn('no-store', head['Cache-Control'])
        self.assertEqual(self.client.post('/sw.js').status_code, 405)

    def test_head_response_carries_no_body(self):
        """线上实测：给 HEAD 输完整 body 会被 gunicorn 按 RFC 9110 丢弃并记一条告警，
        每次发布检查都在日志里留噪声。

        注意不能写成 self.client.head(...).content —— Django 测试客户端自己就会把
        HEAD 的 body 抹掉（变异反证过：视图返回完整 body 时它依旧报 b''，那会是条空锁），
        所以直接拿 RequestFactory 调视图本体。
        """
        from core.views import service_worker
        resp = service_worker(RequestFactory().head('/sw.js'))
        self.assertEqual(resp.content, b'', 'HEAD 不得带 body')
        self.assertEqual(int(resp['Content-Length']),
                         len(self.client.get('/sw.js').content),
                         '但 Content-Length 要跟 GET 一致，才是合法的 HEAD 响应')

    def test_sw_response_is_not_cacheable(self):
        resp = self.client.get('/sw.js')
        self.assertIn('no-store', resp['Cache-Control'],
                      'no-store 必须在这个视图里显式给，不能指望 nginx 兜')
        self.assertEqual(resp['Service-Worker-Allowed'], '/')

    def test_root_route_serves_the_static_file_itself(self):
        """根路径下发的必须是 static/sw.js 本体：拷第二份就一定会漂移"""
        body = self.client.get('/sw.js').content.decode()
        self.assertEqual(body.strip(), self._sw_source().strip())
        self.assertRegex(body, r"CACHE_VERSION = 'personal-assistant-v\d+")

    def test_rendered_page_registers_root_sw_and_unregisters_legacy(self):
        html = self.client.get(reverse('login')).content.decode()
        self.assertIn("serviceWorker.register('/sw.js')", html,
                      '注册必须是渲染后的根路径，不能改回 /static/sw.js')
        self.assertNotIn("'/static/sw.js'", html, '不得以任何形式把 /static/sw.js 当注册地址')
        # 迁移动作必须枚举后按 scope 筛。单数版 getRegistration('/static/') 在遗留注册
        # 被清掉之后会返回根注册，拿它 unregister 等于每次开页自删根注册，
        # SW 从此不再接管导航（本地实测中真的因此导致离线降级失效）
        self.assertIn('getRegistrations()', html, '迁移必须枚举全部注册')
        self.assertIn("reg.scope.indexOf('/static/')", html, '迁移必须按 scope 精确认领遗留注册')
        self.assertNotIn("getRegistration('/static/')", html,
                         '不得用单数版 getRegistration 做迁移，会误注销根注册')

    def test_sw_excludes_itself_before_any_respond_with(self):
        handler = self._fetch_handler()
        self.assertRegex(handler, r"pathname === '/sw\.js'", 'SW 必须放行自己的脚本请求')
        # 位置也要锁：自放行要是写到某个 respondWith 之后，对已命中分支的请求等于没写
        self.assertLess(handler.index("'/sw.js'"), handler.index('respondWith'))

    def test_sw_skips_non_get_before_any_respond_with(self):
        handler = self._fetch_handler()
        self.assertRegex(handler, r"request\.method !== 'GET'")
        self.assertLess(handler.index("'GET'"), handler.index('respondWith'))

    def test_page_offline_fallback_is_navigation_only(self):
        handler = self._fetch_handler()
        self.assertIn("request.mode === 'navigate'", handler)
        # 旧版还用了「Accept 含 text/html」当判据，而 HTMX 片段请求正好也带这个头，
        # 会被当成页面缓存。锁机制而不是锁 text/html 子串：离线降级页的 Content-Type 合法含它
        self.assertNotIn("headers.get('accept')", handler,
                         '不得用 Accept 判 HTML，会误伤 HTMX 片段请求')

    def test_css_and_js_are_not_cache_first(self):
        """CSS/JS 走 cache-first 的话，改样式就又要靠「记得升版本号」，这条正是本次要根除的"""
        branch = self._branch('/static/css/')
        self.assertIn('networkFirst(', branch, 'CSS/JS 分支必须网络优先')
        self.assertNotIn('cacheFirst(', branch, 'CSS/JS 不得回到 cache-first')
        # 相邻的「其余静态资源」分支仍应是 cache-first（图标体积稳定，不需要每问一次网络）
        self.assertIn('cacheFirst(', self._branch("startsWith('/static/')"))

    def test_media_is_not_cached(self):
        """/media/ 必须直接 return：用户上传同名覆盖文件后不该继续看到旧内容"""
        line = next(l for l in self._fetch_handler().splitlines() if "'/media/'" in l)
        self.assertIn('return', line)
        self.assertNotIn('respondWith', line, '/media/ 不得被拦接缓存')

    def _cache_put_body(self):
        """cachePut 函数体，剔掉注释行：上面的注释里就提了 caches.open，
        不剔掉做位置比较时会把注释文本当成代码，假失败"""
        src = self._sw_source()
        start = src.index('function cachePut')
        body = src[start:src.index('\n}', start) + 2]
        return '\n'.join(l for l in body.splitlines() if not l.strip().startswith('//'))

    def test_runtime_refill_survives_body_handoff(self):
        """运行时回填的两条时序约束，违反任何一条都是静默失败

        本地实测时两条全踩过：写完之后缓存里始终只有 precache 那几条，
        页面导航 HTML 根本进不了缓存，导致离线兜底形同虚设。
          1. response.clone() 必须同步做。留到 caches.open().then() 里，回调跑起来时
             原响应已经被 respondWith 接手开始往页面流，clone() 直接抛
             「TypeError: Response body is already used」。
          2. put 必须挂在 event.waitUntil() 上，否则 SW 可能在写入前被回收。
        """
        body = self._cache_put_body()
        self.assertIn('response.clone()', body)
        self.assertLess(body.index('response.clone()'), body.index('caches.open('),
                        'clone() 必须在进入 caches.open 的异步回调之前同步做完')
        self.assertLess(body.index('response.clone()'), body.index('.then('),
                        'clone() 不能写在 .then 回调里')
        self.assertIn('event.waitUntil(', body, '回填必须挂在 waitUntil 上')
        self.assertRegex(body, r'cache\.put\(key \|\| request, clone\)',
                         '交给 cache.put 的必须是那个 clone（key 为空时退回原 request）')
        self.assertNotIn('cache.put(request, response', body,
                         '不能把原响应本身交给 cache.put')
        self.assertNotIn('cache.put(request, clone', body,
                         '静态资源必须按剔了版本号的 key 存，否则与 PRECACHE_URLS 的裸路径对不上')

    def test_static_cache_key_strips_the_version_query(self):
        """静态分支必须拿不带查询串的 key 去存取

        模板给 CSS/JS 加了 ?v=<内容哈希>（为了绕开浏览器 HTTP 缓存的启发式新鲜度），
        而 PRECACHE_URLS 写的是裸路径。两边对不上的话不会报错，只会默默 miss：
        断网时页面能开但完全没样式（离线兜底形同虚设的另一种形式）。
        """
        handler = self._fetch_handler()
        self.assertIn("new Request(url.origin + url.pathname", handler,
                      '静态缓存键必须只由 pathname 构造（不能用 url.href 或原始 request）')
        for marker in ("startsWith('/static/css/')", "startsWith('/static/')"):
            branch = self._branch(marker)
            self.assertRegex(branch, r'(networkFirst|cacheFirst)\(event, request,.*staticKey\)',
                             f'{marker} 分支没把 staticKey 传下去，离线会 miss 预缓存条目')

    def test_page_navigation_cache_key_keeps_the_query(self):
        """反过来：页面导航的查询串是语义的一部分，不能一并剥掉

        ?page=2 / ?tag=自驾 / ?q=xxx 都是不同内容，剥掉查询串会把筛选结果串页。
        """
        branch = self._branch("request.mode === 'navigate'")
        self.assertIn('networkFirst(event, request,', branch,
                      '导航分支必须继续用原始 request 做缓存键')
        self.assertNotIn('staticKey', branch, '剥查询串的处理不能泄漏到页面导航上')


class DashboardDesktopLayoutTest(TestCase):
    """仪表盘桌面两列布局回归锁（本轮从整宽单列改为「主内容流 + 辅助右列」）

    为什么锁：两列完全靠通用列容器 .page-cols + 模板里两个列实现，退化时没有任何报错。
    移动端契约：右列（报告入口 · 活动分布）整块排在主内容流之后，所以 DOM 里的块顺序
    就是移动端顺序契约 —— 报告入口相对改造前从第 2 块下移到页底，是本页唯一的顺序变化。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'core' / 'dashboard.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        # 「活动分布」是条件渲染块（stats.status_counts），没数据整块不出现，顺序锁会空跑
        Activity.objects.create(user=self.user, name='新西兰之旅', status='in_progress')
        Activity.objects.create(user=self.user, name='晨读', status='done')
        Conversation.objects.create(user=self.user, title='面签准备')
        self.html = self.client.get('/dashboard/').content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('本周完成', '统计卡'), ('id="dashWeekChart"', '本周费用图'),
                  ('最近活动', '最近活动卡'), ('最近对话', '最近对话卡')],
            right=[('生成报告', '报告入口卡'), ('活动分布', '状态分布卡')],
            mobile_order=['本周完成', 'id="dashWeekChart"', '最近活动', '生成报告', '活动分布'])

    def test_rail_never_empty_without_conversations(self):
        """没对话也不能空着右列：报告入口卡是无条件渲染的（活动分布卡才是条件块）"""
        Conversation.objects.all().delete()
        html = self.client.get('/dashboard/').content.decode()
        self.assertIn('生成报告', html)
        self.assertIn('class="page-rail', html)


class WeeklyReportDesktopLayoutTest(TestCase):
    """周/月/年报告页桌面两列布局回归锁（正文在左、报告去向在右）

    统计带按口径留在两列区之上（原本就是横排四卡），所以它不在任何一列切片里，
    只用移动端顺序锚点断言它仍在正文之前。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'core' / 'weekly_report.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        Activity.objects.create(user=self.user, name='新西兰之旅', status='done')
        self.html = self.client.get('/reports/weekly/').content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('id="report-body"', '报告正文卡'),
                  ('prose prose-sm max-w-2xl', '正文限宽（可读行长不跟着列宽跑）')],
            right=[('报告去向', '操作卡标题'), ('保存到知识库', '存知识库入口'),
                   ('推送到对话', '推对话入口')],
            mobile_order=['活动总数', 'id="report-body"', '报告去向'])

    def test_report_forms_survive_the_move_into_rail(self):
        """两个 form 搬进右列后协议不能变：action / 隐藏字段 / csrf 都在

        只锁模板结构，不去发 POST：report_send_to_chat 要求已有一个 idle 会话，
        没会话时返 400，拿它做布局锁会变成在测无关的业务分支。"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        self.assertIn('<form method="post">', src, '存知识库的 form 丢了 action 前的原写法')
        self.assertIn("action=\"{% url 'report_send_to_chat' %}\"", src)
        self.assertEqual(src.count('name="content"'), 2, '两个表单的 content 隐藏字段缺一')
        self.assertEqual(src.count('{% csrf_token %}'), 2, '右列里的 csrf 丢了就只会被 403')


class DesktopLayoutCoverageTest(TestCase):
    """全站桌面两列覆盖面政策锁（不渲染页面，只扫模板源）

    为什么锁：两列口径是「除少数天然单列页外，所有页面桌面端至少左右两列」，
    这条约定本身没有任何机制守住 —— 后来的人可能把某页改回单列、给某页另抄
    一份 grid、或者把决定保持单列的页面（登录/表单/聊天详情/日历/宽表）顺手拆了。
    所以这里把「哪些页面是两列」与「哪些页面刻意保持单列」两份名单钉死。
    """
    TEMPLATES = Path(settings.BASE_DIR) / 'templates'
    CSS = Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css'

    # 走通用列容器的页面（口径：左列=主内容流，右列=辅助信息/概览/操作入口）
    TWO_COLUMN = [
        'activities/activity_detail.html',
        'core/dashboard.html', 'core/weekly_report.html',
        'activities/expense_report.html',
        'knowledge/article_list.html', 'knowledge/article_detail.html',
        'notes/note_list.html', 'memory/memory_list.html',
    ]
    # 刻意保持单列：宽表/宽网格、单一任务页、等分双组页
    SINGLE_COLUMN = {
        'activities/activity_list.html': '宽表格 min-w-[760px]，吃满整宽才放得下',
        'activities/activity_calendar.html': '月/周视图是 7 列网格，压进左列每格不足 123px',
        'activities/next_actions.html': '单列待办列表，无辅助信息列可提',
        'activities/activity_form.html': '纯表单编辑页，输入宽度就是舒适宽度',
        'knowledge/article_form.html': '纯表单编辑页',
        'chat/conversation_detail.html': '消息流是单一线性时间轴，右列无天然内容',
        'chat/conversation_list.html': '分栏布局用 .chat-layout 而非通用 .page-cols',
        'registration/login.html': '居中构图 + 全站唯一品牌位，没有辅助信息可提',
    }

    def test_two_column_pages_all_use_the_generic_container(self):
        for rel in self.TWO_COLUMN:
            src = (self.TEMPLATES / rel).read_text(encoding='utf-8')
            self.assertEqual(src.count('class="page-cols'), 1,
                             f'{rel} 应有一个通用两列容器（0=被改回单列，2=抄了第二份）')

    def test_names_listed_as_two_column_are_not_stale(self):
        """名单里写了但实际没有列容器的页 = 名单在骗后来的人"""
        for rel in self.TWO_COLUMN:
            self.assertTrue((self.TEMPLATES / rel).exists(), f'{rel} 已不存在，名单该清一清')

    def test_pages_kept_single_column_have_no_generic_container(self):
        for rel, reason in self.SINGLE_COLUMN.items():
            src = code_only((self.TEMPLATES / rel).read_text(encoding='utf-8'))
            self.assertNotIn('page-cols', src,
                             f'{rel} 是刻意保持单列的页面（{reason}），不要顺手拆两列')

    def test_grid_declaration_exists_once_project_wide(self):
        """整份 custom.css 里这套列宽只能有一处声明"""
        css = self.CSS.read_text(encoding='utf-8')
        owners = [m.group(1).strip() for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css)
                  if 'grid-template-columns: minmax(0, 1fr) 320px' in m.group(2)]
        self.assertEqual(owners, ['.page-cols'],
                         '有人另抄了一份列声明：改一处不会连带另一处')

    def test_no_template_defines_its_own_column_widths(self):
        """模板里禁止内联 grid-template-columns（列宽只由 custom.css 决定）"""
        offenders = []
        for tpl in self.TEMPLATES.rglob('*.html'):
            if 'grid-template-columns' in tpl.read_text(encoding='utf-8'):
                offenders.append(tpl.name)
        self.assertEqual(offenders, [], f'这些模板内联了列宽：{offenders}')


class StaticAssetVersionTest(TestCase):
    """静态资源内容版本号锁

    真实故障：全站两列化上线后，用户在活动详情页看到「快捷操作」掉到页底 ——
    新 HTML + 旧 CSS：.page-cols 拿不到 grid 声明就退化成块级流，右列整块排到主内容之后。
    根因是 nginx 的 location /static/ 不下发任何 Cache-Control，浏览器按启发式新鲜度
    （约 (now - Last-Modified) × 10%）直接复用磁盘里的旧文件，连网络都不走；
    SW 的 network-first 用的也是这条 fetch()，同样被 HTTP 缓存答回来。
    所以 CSS/JS 的 URL 必须带内容哈希 —— 手动升版本号迟早会忘，忘了就是「发布后样式滞后」。
    """
    TEMPLATES = Path(settings.BASE_DIR) / 'templates'
    BARE_REF = re.compile(r"""\{%\s*static\s+'(css|js)/[^']+'\s*%\}""")
    VERSIONED_REF = re.compile(r"""\{%\s*staticv\s+'([^']+)'\s*%\}""")

    def test_no_bare_local_css_js_reference(self):
        offenders = []
        for tpl in self.TEMPLATES.rglob('*.html'):
            hits = self.BARE_REF.findall(tpl.read_text(encoding='utf-8'))
            if hits:
                offenders.append(tpl.name)
        self.assertEqual(offenders, [],
                         '这些模板用了裸 static 标签引本地 CSS/JS，改了不会自动失效：'
                         + str(offenders))

    CORE_TAGS = ('staticv', 'ai_markdown')

    def test_every_template_using_core_tags_loads_them(self):
        """漏 load core_tags 不是「样式旧一点」，是整页 500（TemplateSyntaxError）

        按标签逐个查而不是只查 staticv：后来加的 ai_markdown 同样会踩这个坑。
        """
        offenders = []
        for tpl in self.TEMPLATES.rglob('*.html'):
            src = tpl.read_text(encoding='utf-8')
            # load 行按 Django 模板规则只出现在 {% block %} 之前，扫前 6 行足够
            head = '\n'.join(src.splitlines()[:6])
            used = [t for t in self.CORE_TAGS if ('|' + t) in src or ('{%% %s ' % t) in src
                    or ('{%% %s\'' % t) in src]
            if used and 'core_tags' not in head:
                offenders.append('%s(%s)' % (tpl.name, ','.join(used)))
        self.assertEqual(offenders, [], '用了 core_tags 里的标签却没在顶部 load core_tags：' + str(offenders))

    def test_rendered_page_carries_content_derived_token(self):
        from core.templatetags.core_tags import source_token
        html = self.client.get('/accounts/login/').content.decode()
        token = re.search(r'custom\.css\?v=([0-9a-f]+)', html)
        self.assertTrue(token, '渲染出的 CSS URL 没带版本号')
        self.assertEqual(len(token.group(1)), 10)
        self.assertEqual(token.group(1), source_token(str(settings.BASE_DIR / 'static/css/custom.css')),
                         '版本号必须由文件内容算出，否则改了文件 URL 不变')

    def test_token_changes_with_content_not_with_mtime(self):
        """内容相同 → 同一 token（哪怕 mtime 变了）；内容变了 → token 必须变"""
        import os
        import tempfile
        from core.templatetags.core_tags import source_token
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, 'a.css')
            with open(a, 'w') as fh:
                fh.write('.page-cols{display:grid}')
            first = source_token(a)
            os.utime(a, (0, 0))                     # 只改时间不改内容
            self.assertEqual(source_token(a), first, '只改 mtime 就换 token 会让缓存天天失效')
            with open(a, 'w') as fh:
                fh.write('.page-cols{display:block}')
            self.assertNotEqual(source_token(a), first, '改内容必须换 token')

    def test_missing_source_degrades_to_plain_url(self):
        """找不列源文件（未 collectstatic / 路径写错）时退回裸 URL，不能渲染报错"""
        from core.templatetags.core_tags import static_versioned
        url = static_versioned('css/does-not-exist.css')
        self.assertNotIn('?v=', url)
        self.assertIn('does-not-exist.css', url)


class AiMarkdownRenderTest(SimpleTestCase):
    """AI 回复的 Markdown 渲染器

    核心口径：**先转义、后解析**。模型输出的任何 HTML 都只能是文字，伪链接被 scheme
    白名单拦掉。渲染失败一律降级成纯文本（AGENTS.md 容错铁律），绝不吞掉正文。
    """

    def r(self, text):
        from core.markdown_render import render_markdown
        return str(render_markdown(text))

    # ── 基础排版 ──

    def test_inline_styles(self):
        out = self.r('**粗** 和 *斜* 和 `码`')
        self.assertIn('<strong>粗</strong>', out)
        self.assertIn('<em>斜</em>', out)
        self.assertIn('<code class="md-code">码</code>', out)

    def test_heading_level_mapping(self):
        """正文里不出现 h1（页面标题已经是 h1，否则一个回复能造出第二个主标题）

        口径是「只夹 h1」而不是「整体降一级」：模型写小节习惯从 `##` 起头，整体降级会
        把真正的小节推到 h3（字号几乎与正文齐平）。`###` 才是 h3。
        """
        self.assertIn('<h2 class="md-h"', self.r('# 一级'))
        self.assertIn('<h2 class="md-h"', self.r('## 小结'))
        self.assertIn('<h3 class="md-h"', self.r('### 细项'))
        self.assertIn('<h6 class="md-h"', self.r('###### 六级'))
        self.assertNotIn('<h1', self.r('# 一级\n###### 六级'))
        # 7 个 # 不是标题（CommonMark 口径），整行当普通文字输出；防止把 #{1,6} 改松成 #+
        self.assertNotIn('<h', self.r('####### 越级'))

    def test_single_newline_becomes_br_and_blank_line_splits_paragraphs(self):
        out = self.r('第一行\n第二行\n\n下一段')
        self.assertIn('第一行<br>第二行', out)
        self.assertEqual(out.count('<p class="md-p"'), 2)

    def test_lists(self):
        out = self.r('- 甲\n- 乙')
        self.assertIn('<ul class="md-list">', out)
        self.assertEqual(out.count('<li>'), 2)
        out = self.r('1. 一\n2. 二')
        self.assertIn('<ol class="md-list">', out)
        nested = self.r('- 甲\n  - 子甲\n- 乙')
        self.assertEqual(nested.count('<ul class="md-list">'), 2, '缩进两格应开一层嵌套')

    def test_table_with_inline_marks_in_cells(self):
        out = self.r('| 日期 | 价 |\n|---|---|\n| 8-26 | **4592** |')
        self.assertIn('<table class="md-table">', out)
        self.assertIn('<th>日期</th>', out)
        self.assertIn('<td><strong>4592</strong></td>', out)

    def test_fenced_code_keeps_markers_literal(self):
        out = self.r('```\n**不是粗体**\n```')
        self.assertIn('<pre class="md-pre"><code>**不是粗体**</code></pre>', out)
        self.assertNotIn('<strong>', out)

    def test_blockquote_after_escaping(self):
        """`>` 在 escape 之后是 &gt;，正则必须按转义形态匹配，否则引用永远识别不出来"""
        self.assertIn('<blockquote class="md-quote">引用</blockquote>', self.r('> 引用'))

    def test_links(self):
        out = self.r('[官网](https://example.com/a?x=1&y=2)')
        self.assertIn('href="https://example.com/a?x=1&amp;y=2"', out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertIn('target="_blank"', out)
        bare = self.r('参考 https://example.com/x')
        self.assertIn('<a class="md-link md-break" href="https://example.com/x"', bare)

    def test_result_is_safe_string(self):
        """必须是 SafeString，否则模板会把我生成的标签再转义一遍，页面上就是一堆源码"""
        from django.utils.safestring import SafeString
        from core.markdown_render import render_markdown
        self.assertIsInstance(render_markdown('**粗**'), SafeString)

    def test_empty_and_none(self):
        self.assertEqual(self.r(''), '')
        self.assertEqual(self.r(None), '')

    # ── 安全（这个类的重点）──

    def test_raw_html_is_escaped_not_executed(self):
        out = self.r('<script>alert(1)</script>')
        self.assertNotIn('<script', out)
        self.assertIn('&lt;script&gt;', out)

    def test_img_onerror_attempt_is_inert(self):
        out = self.r('<img src=x onerror=alert(1)>')
        self.assertNotIn('<img', out)
        self.assertIn('&lt;img', out)

    def test_javascript_and_data_urls_are_blocked(self):
        for url in ('javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,<script>alert(1)</script>',
                    'vbscript:msgbox(1)'):
            with self.subTest(url=url):
                out = self.r(f'[点我]({url})')
                self.assertNotIn('href="javascript', out.lower())
                self.assertNotIn('href="data:', out.lower())
                self.assertNotIn('<a ', out, '被拦掉的链接连标记语法都不该变成标签')

    def test_quotes_in_url_cannot_break_out_of_the_attribute(self):
        """用真双引号试从 href 属性里跳出去：escape 阶段已把 " 变成 &quot;，属性封包不可能提前结束"""
        out = self.r('[x](https://example.com/"onmouseover="evil)')
        self.assertIn('&quot;', out, '引号必须仍是转义态')
        self.assertNotIn('onmouseover="', out.replace('&quot;', ''), '不允许出现真正的属性分隔')
        self.assertEqual(out.count('href="'), 1, '只能有一个 href，不能被拆成两个属性')
        # 带空格的形式（`&#34;` 写法）也不能造出新属性
        loose = self.r('[x](https://example.com/&#34; onmouseover=&#34;evil)')
        self.assertNotIn(' onmouseover="', loose)
        self.assertIn('&amp;#34;', loose, '输入里的实体要再转义一层，不能“洗”回引号')

    def test_nul_and_placeholder_chars_are_stripped(self):
        """占位符用 \\x01 定界：输入里若混进这个字符必须先剥掉，否则能伪造出片段注入"""
        out = self.r('前\x010\x01后')
        self.assertIn('前', out)
        self.assertNotIn('<strong>', out)
        self.assertNotIn('\x01', out)

    # ── 降级 ──

    def test_weird_input_never_raises(self):
        nasty = ['|', '|||', '**', '```', '```\n未闭合', '> ', '- ', '1.', '[x](', '](',
                 '\n\n\n', '   ', '#' * 40, '-' * 40, '|a|\n|-|\n|b|\n|c|\n' * 50,
                 '*a' * 500, 'a' * 5000, '>a\n>b\n- c', '`a` `b` `c`']
        for text in nasty:
            with self.subTest(text=text[:20]):
                out = self.r(text)
                self.assertIsInstance(out, str)

    def test_internal_failure_degrades_to_escaped_text(self):
        from unittest.mock import patch
        import core.markdown_render as mr
        with patch('core.markdown_render._inline', side_effect=RuntimeError('boom')):
            out = str(mr.render_markdown('普通**文本**'))
        self.assertIn('普通**文本**', out)
        self.assertIn('<p class="md-p">', out)
        self.assertNotIn('<strong>', out)

    # ── CSS 与接线 ──

    def test_md_css_rules_exist_and_are_well_formed(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')
        for cls in ('.md-body', '.md-p', '.md-h', '.md-list', '.md-table', '.md-link', '.md-quote'):
            self.assertIn(cls, css)
        # 上一版补丁脚本不小心写出 `./* 注释 */`（点号接注释 → 整条规则被浏览器丢掉）
        self.assertNotIn('./*', css, '选择器与注释粘连，规则会被丢弃')
        self.assertEqual(css.count('{'), css.count('}'), '花括号不配对')

    def test_wide_tables_scroll_instead_of_busting_the_bubble(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')
        self.assertIn('overflow-x: auto', css[css.index('.md-table-wrap'):css.index('.md-table-wrap') + 120])
        self.assertIn('overflow-wrap: anywhere', css[css.index('.md-body'):css.index('.md-body') + 200])
        # 只锁 CSS 不够：渲染器不再输出包裹层时 CSS 声明依旧全在，锁会静默空跑
        #（变异反证 M8 就是这么被抬出来的），所以生成侧也要断言
        out = self.r('| 名称 | 说明 |\n| --- | --- |\n| 甲 | 很长的一段说明 |')
        self.assertIn('<div class="md-table-wrap"><table class="md-table">', out)
        self.assertEqual(out.count('</table></div>'), 1, '包裹层必须闭合，否则后面的段落全掉进表格里')

    def test_every_class_the_renderer_emits_has_a_css_rule(self):
        """渲染器发出的每个 md-* 类名都必须在 custom.css 里有规则

        真实漂移：渲染器给裸链挂了 md-break、AGENTS.md 也写了它负责断行，但 CSS 里
        根本没这条规则 —— 类名成了哑弹，只能在真机上看长 URL 没断开才发现。
        逐类名写 assertIn 必然漏新的，所以从生成侧反查。
        """
        import re as re_
        src = (Path(settings.BASE_DIR) / 'core' / 'markdown_render.py').read_text(encoding='utf-8')
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css').read_text(encoding='utf-8')
        emitted = set(re_.findall(r'\bmd-[a-z][a-z-]*', src))
        self.assertTrue(emitted, '一个类名都没扫到说明扫法写错了')
        missing = sorted(c for c in emitted if ('.' + c) not in css)
        self.assertEqual(missing, [], '这些类名由渲染器输出但没有对应样式：%s' % missing)


class AiMarkdownTerminationTest(SimpleTestCase):
    """渲染器必须在有限步内返回

    真实事故：列表分支把匹配正则选反（无序行拿 _OL 去匹），一行也收不到 → `i` 永不
    前进 → 整页请求死循环。这种 bug 不会报错，只会把一个 gunicorn worker 拖死，
    所以除了修掉，还在主循环顶部加了「行号未推进就抛」的守卫（被 except 兜住降级成纯文本）。
    """

    def r(self, text):
        from core.markdown_render import render_markdown
        return str(render_markdown(text))

    def test_lone_list_markers_terminate(self):
        for text in ('- ', '* ', '+ ', '-\n', '1.', '1) ', '  - 缩进项'):
            with self.subTest(text=text):
                self.assertIsInstance(self.r(text), str)

    def test_fixed_list_branch_still_renders_lists(self):
        """修死循环时别把功能一起修没：无序/有序各自成列表，不能互相串"""
        self.assertIn('<ul class="md-list">', self.r('- 甲\n- 乙'))
        self.assertIn('<ol class="md-list">', self.r('1. 甲\n2. 乙'))
        out = self.r('- 甲\n1. 乙')
        self.assertIn('<ul class="md-list">', out)
        self.assertIn('<ol class="md-list">', out)


class FollowUpLineTest(SimpleTestCase):
    """「下一步：A｜B」的解析（可点追问的唯一入口）

    为什么用一行文本而不是协议 JSON 字段：通用问答走的是「非 JSON 透传」分支
    （协议逃生舱），那里没有 JSON 可挂字段，而那恰恰是最需要下一步的长回答。
    """

    def extract(self, text):
        from core.agent_registry import extract_follow_ups
        return extract_follow_ups(text)

    def test_extracts_and_strips_the_trailing_line(self):
        body, items = self.extract('今天金价约 4325 美元。\n\n下一步：查上海金店报价｜把金价记到新西兰旅游')
        self.assertEqual(body, '今天金价约 4325 美元。')
        self.assertEqual(items, ['查上海金店报价', '把金价记到新西兰旅游'])

    def test_tolerates_halfwidth_punctuation_and_brackets(self):
        for text, expected in (
            ('正文\n【下一步】甲|乙', ['甲', '乙']),
            ('正文\n[下一步] 甲、乙', ['甲', '乙']),
            ('正文\n下一步：甲；乙', ['甲', '乙']),
            ('正文\n下一步：甲。', ['甲']),
        ):
            with self.subTest(text=text):
                body, items = self.extract(text)
                self.assertEqual(body, '正文')
                self.assertEqual(items, expected)

    def test_caps_at_three_and_drops_overlong_options(self):
        """选项过长说明模型把解释写成了选项：整条丢掉而不是截断（截断后发回去意思就变了）"""
        _, items = self.extract('正文\n下一步：甲｜乙｜丙｜丁')
        self.assertEqual(items, ['甲', '乙', '丙'])
        _, items = self.extract('正文\n下一步：' + '长' * 41 + '｜乙')
        self.assertEqual(items, ['乙'])

    def test_mid_text_mention_is_not_swallowed(self):
        """正文中间（甚至整条就是）的「下一步：」不得被剥掉：宁可不显示 chips"""
        text = '下一步：先订机票。然后再讨论预算'
        body, items = self.extract(text)
        self.assertEqual(items, [])
        self.assertEqual(body, text)
        # 末行但是散文（没冒号也没括号）：不能当成标记
        prose = '报价查完了。\n下一步是订机票，预算我们回头再谈'
        body, items = self.extract(prose)
        self.assertEqual(items, [])
        self.assertEqual(body, prose)

    def test_line_without_options_is_left_alone(self):
        text = '正文\n下一步：'
        body, items = self.extract(text)
        self.assertEqual((body, items), (text, []))

    def test_protocol_teaches_the_convention(self):
        from core.agent_registry import build_protocol_prompt
        prompt = build_protocol_prompt()
        self.assertIn('最后单独一行', prompt)
        self.assertIn('下一步：', prompt)
        # 反问式选项（“你希望我帮你查吗？”）点下去发出去的是问句，等于没点
        self.assertIn('不要写「你希望我', prompt)

    def test_protocol_says_a_pinned_activity_needs_no_re_query(self):
        """钉选注入的现状必须真的被用起来：真机实测模型仍会走 get 工具确认一遍
        （多一次往返、多一次出错机会，而且“还剩多少”本可以直接算）"""
        from core.agent_registry import build_protocol_prompt
        prompt = build_protocol_prompt()
        self.assertIn('[钉选对象]', prompt)
        self.assertIn('不要为了确认而调 get/query', prompt)

    def test_orchestrator_attaches_chips_on_the_passthrough_path(self):
        from django.contrib.auth import get_user_model
        from core.agent_registry import orchestrator
        user = get_user_model()(username='u')
        content, payload, changed = orchestrator.process(
            user, '查完了，现价约 4325 美元。\n下一步：把金价记到新西兰旅游｜查上海金店报价')
        self.assertEqual(content, '查完了，现价约 4325 美元。')
        self.assertEqual(payload['follow_ups'], ['把金价记到新西兰旅游', '查上海金店报价'])
        self.assertFalse(changed)

    def test_orchestrator_attaches_chips_on_the_json_path(self):
        from django.contrib.auth import get_user_model
        from core.agent_registry import orchestrator
        user = get_user_model()(username='u')
        text = '{"intent": "chitchat", "reply": "好的。\\n下一步：看看本周安排"}'
        content, payload, _ = orchestrator.process(user, text)
        self.assertEqual(content, '好的。')
        self.assertEqual(payload['follow_ups'], ['看看本周安排'])


class ChitchatMemoryExtractionTest(TestCase):
    """AI 主动记忆提取必须覆盖闲聊路径（线上实测回归锁，2026-09-14）

    原实现把 memory 字段解析写在 _dispatch 函数末尾，而闲聊/无工具分支在
    中途就 return 了——AI 按协议在 JSON 里附带的 memory 字段被整段丢弃，
    Memory 表连续多天零新增（AI 口头承诺「会记住」但实际没存）。
    锁：chitchat（无工具）回复带 memory 字段也必须落库。
    """

    def test_chitchat_reply_still_saves_memory_field(self):
        from django.contrib.auth import get_user_model
        from memory.models import Memory
        from core.agent_registry import orchestrator
        user = get_user_model().objects.create(username='u')
        text = ('{"intent": "chitchat", "reply": "好的，我会记住这个安排", '
                '"memory": [{"content": "每周三晚上健身", '
                '"category": "habit", "importance": 6}]}')
        content, _, _ = orchestrator.process(user, text)
        self.assertEqual(content, '好的，我会记住这个安排')
        mem = Memory.objects.filter(user=user, content='每周三晚上健身').first()
        self.assertIsNotNone(mem, 'chitchat 回复的 memory 字段被丢弃了')
        self.assertEqual(mem.category, 'habit')

    def test_non_list_memory_field_is_ignored_safely(self):
        from django.contrib.auth import get_user_model
        from memory.models import Memory
        from core.agent_registry import orchestrator
        user = get_user_model().objects.create(username='u')
        text = '{"intent": "chitchat", "reply": "嗯", "memory": "我是一段文本"}'
        content, _, _ = orchestrator.process(user, text)
        self.assertEqual(content, '嗯')
        self.assertFalse(Memory.objects.filter(user=user).exists())


class TagConfigTest(TestCase):
    """自建 core.Tag 标签体系：scope 隔离 / 停用兼容 / 清洗口径 / 各入口一致"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='测试活动')
        self.expense = Expense.objects.create(
            activity=self.activity, user=self.user, amount=Decimal('99.00'))

    def test_scope_isolation_same_name(self):
        """同名标签跨 scope 是不同实体，互不干扰"""
        apply_tags(self.activity, ['旅行'])
        apply_tags(self.expense, ['旅行'])
        apply_tags(Note.objects.create(user=self.user, content='x'), ['旅行'])
        self.assertEqual(Tag.objects.filter(name='旅行').count(), 3)
        self.assertEqual(
            {t.scope for t in Tag.objects.filter(name='旅行')},
            {'activity', 'expense', 'note'})

    def test_used_tags_filters_by_scope_and_visibility(self):
        """used_tags 只返回该 scope 且该用户可见对象上的标签"""
        apply_tags(self.activity, ['团建'])
        apply_tags(self.expense, ['差旅'])
        self.assertEqual(set(used_tags('activity', self.user).values_list('name', flat=True)),
                         {'团建'})
        self.assertEqual(set(used_tags('expense', self.user).values_list('name', flat=True)),
                         {'差旅'})
        self.assertEqual(used_tags('knowledge', self.user).count(), 0)

    def test_apply_tags_set_semantics_and_clear(self):
        """apply_tags 整体替换，空列表 = 清空"""
        apply_tags(self.activity, ['a', 'b'])
        apply_tags(self.activity, ['b', 'c'])
        self.assertEqual(set(tag_names(self.activity)), {'b', 'c'})
        apply_tags(self.activity, [])
        self.assertEqual(tag_names(self.activity), [])

    def test_add_tags_append_semantics(self):
        """add_tags 追加不覆盖"""
        apply_tags(self.activity, ['a'])
        add_tags(self.activity, ['b', 'a'])
        self.assertEqual(set(tag_names(self.activity)), {'a', 'b'})

    def test_split_tag_names_cleanup(self):
        """中英文逗号/顿号分隔 + 去空去重 + 限量"""
        self.assertEqual(split_tag_names('a，b、c,d , ,a'),
                         ['a', 'b', 'c', 'd'])
        self.assertEqual(split_tag_names(['x', 'y、z']), ['x', 'y', 'z'])
        self.assertEqual(len(split_tag_names([f'n{i}' for i in range(20)])), 10)

    def test_deactivated_tag_reused_not_revived(self):
        """停用标签被显式输入时直接复用（不复活、不重建），建议列表不再推荐"""
        apply_tags(self.activity, ['旧标签'])
        tag = Tag.objects.get(scope='activity', name='旧标签')
        tag.is_active = False
        tag.save()
        self.assertNotIn('旧标签', active_tag_names('activity'))
        # 建议列表仍包含用户用过的停用标签（编辑历史对象时回填不能丢）
        self.assertIn('旧标签', tag_suggestions('activity', self.user))
        # 再写入不创建新行
        apply_tags(self.activity, ['旧标签'])
        self.assertEqual(Tag.objects.filter(scope='activity', name='旧标签').count(), 1)
        tag.refresh_from_db()
        self.assertFalse(tag.is_active)

    def test_suggestions_prebuilt_first_then_used(self):
        """预建启用标签在前（sort 序），用户用过的补后去重"""
        Tag.objects.create(scope='activity', name='预建甲', sort=1)
        Tag.objects.create(scope='activity', name='预建乙', sort=0)
        Tag.objects.create(scope='activity', name='停用丙', sort=2, is_active=False)
        apply_tags(self.activity, ['用过的'])
        names = tag_suggestions('activity', self.user)
        self.assertEqual(names[:2], ['预建乙', '预建甲'])
        self.assertNotIn('停用丙', names[:2])
        self.assertIn('用过的', names)

    def test_tag_suggestions_scope_separated_for_forms(self):
        """费用表单建议只含 expense scope，与活动标签隔离（各入口口径一致）"""
        apply_tags(self.activity, ['活动专属'])
        apply_tags(self.expense, ['费用专属'])
        self.assertIn('活动专属', tag_suggestions('activity', self.user))
        self.assertNotIn('活动专属', tag_suggestions('expense', self.user))
        self.assertIn('费用专属', tag_suggestions('expense', self.user))

    def test_expense_tag_via_add_expense_service(self):
        """AI 记账/视图层共用 add_expense 服务：tags 参数落库为 expense scope"""
        from activities.services import add_expense
        expense = add_expense(self.activity, self.user, '66.5',
                              tags='通勤,地铁')
        self.assertEqual(set(tag_names(expense)), {'通勤', '地铁'})
        self.assertEqual(
            set(used_tags('expense', self.user).values_list('name', flat=True)),
            {'通勤', '地铁'})

    def test_scope_of_rejects_unregistered_model(self):
        """未注册的标签宿主模型直接报错，不静默写错 scope"""
        from core.tags import _scope_of
        with self.assertRaises(ValueError):
            _scope_of(self.user)


class ConfirmCardGlobalTest(SimpleTestCase):
    """全站统一确认卡：原生 confirm() 弹窗全部替换为 data-confirm + 全局组件"""

    @staticmethod
    def _templates():
        base = Path(settings.BASE_DIR) / 'templates'
        return sorted(base.rglob('*.html'))

    def test_no_native_confirm_left(self):
        """不允许任何模板回潮使用原生 onsubmit confirm 弹窗"""
        offenders = []
        for p in self._templates():
            for i, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
                if 'onsubmit="return confirm(' in line:
                    offenders.append(f'{p.relative_to(settings.BASE_DIR)}:{i}')
        self.assertEqual(offenders, [])

    def test_danger_forms_use_confirm_card(self):
        """所有 data-confirm 表单必须带调性与按钮文案三件套；删除类固定 danger+确认删除"""
        total = 0
        for p in self._templates():
            for line in p.read_text(encoding='utf-8').splitlines():
                if 'data-confirm="' not in line:
                    continue
                self.assertIn('data-confirm-tone=', line)
                self.assertIn('data-confirm-label="', line)
                if '删除' in line:
                    self.assertIn('data-confirm-tone="danger"', line)
                    self.assertIn('data-confirm-label="确认删除"', line)
                total += 1
        # 9 个模板内删除表单 + conversation_list.html JS 拼装删除表单
        # + 日历订阅设置页 2 个（重新生成/吊销，2026-09-11）
        # + 活动详情页评论删除 1 个（2026-09-14 评论功能）
        self.assertEqual(total, 13)

    def test_confirm_card_js_exists_and_wired(self):
        """全局组件文件存在（含表单拦截与 paConfirmCard API），且 base.html 已引入"""
        js = (Path(settings.BASE_DIR) / 'static' / 'js' / 'confirm-card.js'
              ).read_text(encoding='utf-8')
        self.assertIn("matches('form[data-confirm]')", js)
        self.assertIn('window.paConfirmCard', js)
        base = (Path(settings.BASE_DIR) / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn("staticv 'js/confirm-card.js'", base)


class ChatLayoutFlexHeightTest(SimpleTestCase):
    """chat 分栏布局的 flex 高度链锁：缺 min-height:0 会被长内容撑爆固定高度容器，
    移动端发送框被底部 Tab 栏遮住（390×844 实测过）"""

    @staticmethod
    def _rule_blocks(css, selector):
        """提取 custom.css 中指定选择器的规则块文本（含多个同名块时逐个检查）"""
        return re.findall(re.escape(selector) + r'\s*\{([^}]*)\}', css)

    def test_flex_children_have_min_height_zero(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css'
               ).read_text(encoding='utf-8')
        for selector in ('.chat-main', '.chat-sidebar'):
            blocks = self._rule_blocks(css, selector)
            self.assertTrue(blocks, f'{selector} 规则块不存在')
            # 复合选择器块（如 [data-view] .chat-main）不含高度属性是正常的，
            # 只要主定义块里锁住 min-height: 0 即可
            self.assertTrue(
                any('min-height: 0' in b for b in blocks),
                f'{selector} 缺 min-height: 0，长内容会撑爆 .chat-layout')


class PickIndexParseTest(SimpleTestCase):
    """候选澄清「第 N 个」序号解析（服务端直达兜底的核心解析）"""

    def test_common_phrases(self):
        from core.agent_registry import parse_pick_index
        cases = {'第一个': 1, '第2个': 2, '2': 2, '第三个': 3, '第二个，计划中的': 2,
                 '第 两 个': 2, '１': 1}
        for text, expect in cases.items():
            self.assertEqual(parse_pick_index(text), expect, text)

    def test_unparseable_or_negative(self):
        from core.agent_registry import parse_pick_index, _pick_from_text
        self.assertIsNone(parse_pick_index('改时间'))
        self.assertIsNone(parse_pick_index('第十一个'))
        self.assertIsNone(_pick_from_text('不要第一个'))
        self.assertIsNone(_pick_from_text('别第二个'))
        self.assertIsNone(_pick_from_text(None))


class QuickChipsTest(TestCase):
    """动态开场 chips（core.chips）：按当日上下文生成对话起头

    线上实测 2026-09-15：固定 chips 天天一样，近 14 天仅 25 条用户消息。
    动态化把「想话题」门槛拿掉——锁住各数据源的生成口径与兜底行为。
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user('chips_user', password='p')

    def _chips(self):
        from core.chips import get_quick_chips
        return get_quick_chips(self.user)

    def test_empty_context_falls_back_to_static(self):
        """空库新用户：退化为固定兜底条，不至于没东西可点"""
        chips = self._chips()
        self.assertEqual(len(chips), 4)
        self.assertIn('今天有什么安排？', chips)
        self.assertIn('你还记得我哪些事？', chips)

    def test_single_today_activity_names_it(self):
        from datetime import timedelta
        from activities.models import Activity
        from django.utils import timezone
        Activity.objects.create(user=self.user, name='牙医复诊',
                                start_date=timezone.localdate(), status='planned')
        chips = self._chips()
        self.assertTrue(any('牙医复诊' in c for c in chips),
                        f'唯一今日活动应点名：{chips}')

    def test_multiple_today_activities_show_count(self):
        from activities.models import Activity
        from django.utils import timezone
        today = timezone.localdate()
        for name in ('晨跑', '周会', '取快递'):
            Activity.objects.create(user=self.user, name=name,
                                    start_date=today, status='planned')
        chips = self._chips()
        self.assertTrue(any('3 个活动' in c for c in chips), chips)

    def test_undone_activities_counted(self):
        from activities.models import Activity
        from django.utils import timezone
        today = timezone.localdate()
        Activity.objects.create(user=self.user, name='进行中', status='in_progress')
        Activity.objects.create(user=self.user, name='过期计划', status='planned',
                                start_date=today - timedelta(days=2))
        chips = self._chips()
        self.assertTrue(any('2 个活动没完成' in c for c in chips), chips)

    def test_recent_conversation_topic_chip(self):
        from chat.models import Conversation
        Conversation.objects.create(user=self.user, session_id='sess_topic',
                                    title='嫉妒的自救方法')
        chips = self._chips()
        self.assertTrue(any('继续聊聊「嫉妒的自救方法」' in c for c in chips), chips)

    def test_archived_or_old_topic_not_picked(self):
        from chat.models import Conversation
        from django.utils import timezone
        from datetime import timedelta
        Conversation.objects.create(user=self.user, session_id='sess_arch',
                                    title='很久以前的话题', status='archived')
        old = Conversation.objects.create(user=self.user, session_id='sess_old',
                                          title='过期话题')
        old.created_at = timezone.now() - timedelta(days=10)
        old.save(update_fields=['created_at'])
        chips = self._chips()
        self.assertFalse(any('继续聊聊' in c for c in chips), chips)

    def test_superuser_isolation(self):
        """chips 只看自己的数据（超管也不把别人的日程灌进自己的 chips）"""
        from django.contrib.auth import get_user_model
        from activities.models import Activity
        from django.utils import timezone
        other = get_user_model().objects.create_user('other_user', password='p')
        Activity.objects.create(user=other, name='别人的活动',
                                start_date=timezone.localdate(), status='planned')
        chips = self._chips()
        self.assertFalse(any('别人的活动' in c for c in chips), chips)


VAPID_DUMMY = {
    'VAPID_PUBLIC_KEY': 'BTest_public_key',
    'VAPID_PRIVATE_KEY': 'Test_private_key',
}


class WebPushTest(TestCase):
    """Web Push（VAPID）每日早报：订阅端点 + payload 组装 + 发送与清理

    主动触达是产品最大结构空缺（线上审计：用户不打开站就收不到任何信息）。
    锁住：幂等订阅、VAPID 双保险、失效订阅清理、cron 命令只发订阅者。
    """

    ENDPOINT = 'https://fcm.googleapis.com/fcm/send/abc'
    SUB_BODY = {
        'endpoint': ENDPOINT,
        'keys': {'p256dh': 'k' * 40, 'auth': 'a' * 20},
    }

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user('push_user', password='p')
        self.client = Client()
        self.client.force_login(self.user)

    def _subscribe(self, body=None):
        from django.urls import reverse
        return self.client.post(
            reverse('push_subscribe'), data=json.dumps(body or self.SUB_BODY),
            content_type='application/json')

    def test_subscribe_creates_and_is_idempotent(self):
        from core.models import PushSubscription
        resp = self._subscribe()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        self.assertEqual(PushSubscription.objects.count(), 1)
        sub = PushSubscription.objects.get()
        self.assertEqual(sub.user, self.user)

        # 同一浏览器重复订阅（重装 SW/重开页面）：幂等更新而非新建
        new_body = {**self.SUB_BODY,
                    'keys': {'p256dh': 'new' * 20, 'auth': 'b' * 20}}
        self._subscribe(new_body)
        self.assertEqual(PushSubscription.objects.count(), 1)
        self.assertEqual(PushSubscription.objects.get().p256dh, 'new' * 20)

    def test_subscribe_requires_vapid_configured(self):
        """VAPID 未配置 → 403（前端入口已隐藏，这里是服务端双保险）"""
        from django.test import override_settings
        with override_settings(VAPID_PUBLIC_KEY='', VAPID_PRIVATE_KEY=''):
            resp = self._subscribe()
        self.assertEqual(resp.status_code, 403)

    def test_subscribe_rejects_bad_payload(self):
        from django.urls import reverse
        for body in ('not json', {'endpoint': 'x'},
                     {'endpoint': 'x', 'keys': {}}):
            resp = self.client.post(
                reverse('push_subscribe'),
                data=body if isinstance(body, str) else json.dumps(body),
                content_type='application/json')
            self.assertEqual(resp.status_code, 400, body)

    def test_unsubscribe_only_own(self):
        """退订只删自己的记录；别人拿同 endpoint 打接口删不掉（endpoint 全局唯一）"""
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from core.models import PushSubscription
        PushSubscription.objects.create(
            user=self.user, endpoint=self.ENDPOINT,
            p256dh='k' * 40, auth='a' * 20)
        other = get_user_model().objects.create_user('push_other', password='p')

        client2 = Client()
        client2.force_login(other)
        resp = client2.post(
            reverse('push_unsubscribe'),
            data=json.dumps({'endpoint': self.ENDPOINT}),
            content_type='application/json')
        self.assertEqual(resp.json()['deleted'], 0)
        self.assertTrue(PushSubscription.objects.filter(endpoint=self.ENDPOINT).exists())

        resp = self._post_json(reverse('push_unsubscribe'), {'endpoint': self.ENDPOINT})
        self.assertEqual(resp.json()['deleted'], 1)
        self.assertFalse(PushSubscription.objects.exists())

    def _post_json(self, url, data):
        return self.client.post(url, data=json.dumps(data),
                                content_type='application/json')

    def test_push_test_view_reports_sent_count(self):
        from unittest.mock import patch
        from django.urls import reverse
        with patch('core.push.send_push_to_user', return_value=(1, 0)) as mock_send:
            resp = self.client.post(reverse('push_test'))
        self.assertTrue(resp.json()['ok'])
        self.assertEqual(mock_send.call_count, 1)

        with patch('core.push.send_push_to_user', return_value=(0, 0)):
            resp = self.client.post(reverse('push_test'))
        self.assertFalse(resp.json()['ok'])

    def test_daily_payload_variants(self):
        """payload 与 chips 同口径：今日点名/计数、未完成计数、空日兜底"""
        from datetime import timedelta
        from django.utils import timezone
        from activities.models import Activity
        from core.push import build_daily_payload

        payload = build_daily_payload(self.user)
        self.assertIn('今天暂无日程安排', payload['body'])
        self.assertEqual(payload['url'], '/')

        Activity.objects.create(user=self.user, name='牙医复诊',
                                start_date=timezone.localdate(), status='planned')
        payload = build_daily_payload(self.user)
        self.assertIn('「牙医复诊」', payload['body'])

        Activity.objects.create(user=self.user, name='晨跑',
                                start_date=timezone.localdate(), status='planned')
        Activity.objects.create(user=self.user, name='进行中', status='in_progress')
        payload = build_daily_payload(self.user)
        self.assertIn('2 个活动', payload['body'])
        self.assertIn('1 个活动还没完成', payload['body'])

        # 已取消不算日程（与 chips 同口径排除 cancelled）
        Activity.objects.create(user=self.user, name='取消了的',
                                start_date=timezone.localdate(), status='cancelled')
        payload = build_daily_payload(self.user)
        self.assertNotIn('取消了的', payload['body'])

        # 数据隔离：别人的活动不进自己的早报
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user('push_other2', password='p')
        Activity.objects.create(user=other, name='别人的安排',
                                start_date=timezone.localdate(), status='planned')
        self.assertNotIn('别人的安排', build_daily_payload(self.user)['body'])

    @override_settings(**VAPID_DUMMY)
    def test_send_push_ok_updates_last_sent(self):
        from datetime import datetime
        from unittest.mock import patch
        from core.models import PushSubscription
        from core.push import send_push_to_user
        sub = PushSubscription.objects.create(
            user=self.user, endpoint=self.ENDPOINT,
            p256dh='k' * 40, auth='a' * 20)
        with patch('core.push.webpush') as mock_webpush:
            sent, cleaned = send_push_to_user(self.user, {'title': 't', 'body': 'b'})
        self.assertEqual((sent, cleaned), (1, 0))
        self.assertEqual(mock_webpush.call_count, 1)
        sub.refresh_from_db()
        self.assertIsNotNone(sub.last_sent_at)
        # VAPID 私钥与 subject 从 settings 透传给 pywebpush
        kwargs = mock_webpush.call_args.kwargs
        self.assertEqual(kwargs['vapid_private_key'], VAPID_DUMMY['VAPID_PRIVATE_KEY'])
        self.assertEqual(kwargs['vapid_claims']['sub'], settings.VAPID_SUBJECT)

    @override_settings(**VAPID_DUMMY)
    def test_send_push_410_cleans_expired_subscription(self):
        """404/410 = 订阅已失效（清了浏览器数据/取消授权），自动删记录"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from pywebpush import WebPushException
        from core.models import PushSubscription
        from core.push import send_push_to_user
        PushSubscription.objects.create(
            user=self.user, endpoint=self.ENDPOINT,
            p256dh='k' * 40, auth='a' * 20)
        exc = WebPushException('gone')
        exc.response = SimpleNamespace(status_code=410)
        with patch('core.push.webpush', side_effect=exc):
            sent, cleaned = send_push_to_user(self.user, {'title': 't', 'body': 'b'})
        self.assertEqual((sent, cleaned), (0, 1))
        self.assertFalse(PushSubscription.objects.exists())

    @override_settings(**VAPID_DUMMY)
    def test_send_push_other_error_keeps_subscription(self):
        """非 404/410 的发送失败（网络抖动等）不误删订阅"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from pywebpush import WebPushException
        from core.models import PushSubscription
        from core.push import send_push_to_user
        PushSubscription.objects.create(
            user=self.user, endpoint=self.ENDPOINT,
            p256dh='k' * 40, auth='a' * 20)
        exc = WebPushException('boom')
        exc.response = SimpleNamespace(status_code=503)
        with patch('core.push.webpush', side_effect=exc):
            sent, cleaned = send_push_to_user(self.user, {'title': 't', 'body': 'b'})
        self.assertEqual((sent, cleaned), (0, 0))
        self.assertTrue(PushSubscription.objects.exists())

    @override_settings(**VAPID_DUMMY)
    def test_send_push_network_error_isolated_per_subscription(self):
        """单条订阅的网络层异常（如国内连不上 FCM 的 ConnectionError）

        不是 WebPushException：原实现直接炸掉整轮 send_push_to_user，
        后面的订阅收不到、last_sent_date 落不了库 → 每 5 分钟重发（线上实测）。
        修后：失败订阅记 'error'，其余订阅正常送达。
        """
        from unittest.mock import patch
        from core.models import PushSubscription
        from core.push import send_push_to_user
        sub_a = PushSubscription.objects.create(
            user=self.user, endpoint=self.ENDPOINT,
            p256dh='k' * 40, auth='a' * 20)
        sub_b = PushSubscription.objects.create(
            user=self.user, endpoint=self.ENDPOINT + 'b',
            p256dh='k' * 40, auth='a' * 20)
        with patch('core.push.webpush',
                   side_effect=[ConnectionError('fcm unreachable'), None]):
            sent, cleaned = send_push_to_user(self.user, {'title': 't', 'body': 'b'})
        self.assertEqual((sent, cleaned), (1, 0))
        # 无订阅被误删；恰好一条送达（不依赖遍历顺序）
        self.assertEqual(PushSubscription.objects.count(), 2)
        self.assertEqual(
            PushSubscription.objects.filter(last_sent_at__isnull=False).count(), 1)

    def test_base_template_push_entry(self):
        """VAPID 已配置 → 顶栏铃铛 + meta 公钥；未配置 → 入口完全不渲染"""
        from django.test import override_settings
        from django.urls import reverse
        url = reverse('notes:note_list')
        with override_settings(**VAPID_DUMMY):
            html = self.client.get(url).content.decode()
        self.assertIn('push-toggle-btn', html)
        self.assertIn('name="vapid-key" content="BTest_public_key"', html)
        self.assertIn('js/push.js', html)

        with override_settings(VAPID_PUBLIC_KEY='', VAPID_PRIVATE_KEY=''):
            html = self.client.get(url).content.decode()
        self.assertNotIn('push-toggle-btn', html)
        self.assertNotIn('js/push.js', html)


class PushScheduleTest(TestCase):
    """可配置推送计划：内容类型 × 时间点，admin 即改即生效

    每天不限一次 —— 想几次建几条计划（如 8 点早报 + 20 点提醒），
    命令幂等保证每条计划每天最多发一次。
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user('sched_user', password='p')

    def _act(self, name, status='planned', start_date=None):
        from activities.models import Activity
        from django.utils import timezone
        Activity.objects.create(
            user=self.user, name=name, status=status,
            start_date=start_date or timezone.localdate())

    def _sched(self, push_type='daily_brief', time=None, **kw):
        from django.utils import timezone
        from core.models import PushSchedule
        return PushSchedule.objects.create(
            user=self.user, push_type=push_type,
            time=time or timezone.localtime().time(), **kw)

    def _run(self):
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command
        out = StringIO()
        with patch('core.management.commands.send_scheduled_push.build_payload',
                   return_value={'title': 't', 'body': 'b', 'url': '/'}) as mp, \
                patch('core.management.commands.send_scheduled_push.send_push_to_user',
                      return_value=(1, 0)) as ms:
            call_command('send_scheduled_push', stdout=out)
        return mp, ms, out.getvalue()

    # ---------- 内容类型 ----------

    def test_payload_dispatcher_and_unknown_fallback(self):
        from core.push import build_payload
        self.assertTrue(build_payload(self.user, 'task_reminder')['title']
                        .endswith('任务提醒'))
        self.assertTrue(build_payload(self.user, 'daily_brief')['title']
                        .endswith('今日早报'))
        # 未知类型退回今日早报，不炸
        self.assertTrue(build_payload(self.user, 'whatever')['title']
                        .endswith('今日早报'))

    def test_task_reminder_lists_undone(self):
        from datetime import timedelta
        from django.utils import timezone
        from core.push import build_payload
        self._act('改简历', status='in_progress')
        self._act('过期计划', start_date=timezone.localdate() - timedelta(days=1))
        payload = build_payload(self.user, 'task_reminder')
        self.assertIn('2 个任务待处理', payload['body'])
        self.assertIn('「改简历」', payload['body'])
        self.assertTrue(payload['title'].endswith('任务提醒'))
        # 深链直达未完成筛选视图（sw.js notificationclick 按 payload.url 开页）
        self.assertEqual(payload['url'], '/activities/?status=in_progress,planned')

    def test_task_reminder_empty_is_reassuring(self):
        from core.push import build_payload
        self.assertIn('井井有条', build_payload(self.user, 'task_reminder')['body'])

    def test_ai_summary_uses_ai_reply(self):
        from unittest.mock import patch
        from core.push import build_payload
        self._act('牙医复诊')
        with patch('core.ai.ai_round_trip',
                   return_value='今天重点看牙，别的事都可以往后放。') as mock_ai:
            payload = build_payload(self.user, 'ai_summary')
        self.assertEqual(mock_ai.call_count, 1)
        self.assertIn('看牙', payload['body'])
        self.assertTrue(payload['title'].endswith('AI 总结'))

    def test_ai_summary_falls_back_on_ai_exception(self):
        """AI 挂掉/无 Agent 配置 → 规则模板兜底，绝不丢推送"""
        from unittest.mock import patch
        from core.push import build_payload
        self._act('牙医复诊')
        with patch('core.ai.ai_round_trip', side_effect=RuntimeError('down')):
            payload = build_payload(self.user, 'ai_summary')
        self.assertIn('牙医复诊', payload['body'])

        with patch('core.ai.ai_round_trip', return_value=None):
            payload = build_payload(self.user, 'ai_summary')
        self.assertIn('牙医复诊', payload['body'])

    # ---------- 计划触发与幂等 ----------

    def test_command_fires_due_schedule_once(self):
        sched = self._sched()
        _, mock_send, out = self._run()
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(mock_send.call_args.args[0], self.user)
        self.assertIn('due=1', out)
        sched.refresh_from_db()
        from django.utils import timezone
        self.assertEqual(sched.last_sent_date, timezone.localdate())

        # 同一天再扫不再发（幂等）
        _, mock_send2, _ = self._run()
        self.assertEqual(mock_send2.call_count, 0)

    def test_command_skips_future_and_disabled(self):
        from datetime import timedelta
        from django.utils import timezone
        now = timezone.localtime()
        # 23 点后跑测试时 +2h 会跨午夜（当天不再存在「未来时间」），条件创建
        future_dt = now + timedelta(hours=2)
        if future_dt.date() == now.date():
            self._sched(time=future_dt.time())
        self._sched(enabled=False)
        _, mock_send, out = self._run()
        self.assertEqual(mock_send.call_count, 0)
        self.assertIn('due=0', out)

    def test_multiple_schedules_all_fire_same_day(self):
        """每天多次 = 多条计划，一次扫描各自发各自标，互不影响"""
        from datetime import timedelta
        from django.utils import timezone
        self._sched(push_type='daily_brief')
        self._sched(push_type='task_reminder')
        self._sched(push_type='ai_summary')
        _, mock_send, out = self._run()
        self.assertEqual(mock_send.call_count, 3)
        self.assertIn('due=3', out)

        # 全部标记后当天不再重发；次日（模拟 last_sent_date 过期）恢复触发
        _, mock_send2, _ = self._run()
        self.assertEqual(mock_send2.call_count, 0)
        from core.models import PushSchedule
        PushSchedule.objects.update(last_sent_date=timezone.localdate()
                                    - timedelta(days=1))
        _, mock_send3, _ = self._run()
        self.assertEqual(mock_send3.call_count, 3)

    def test_admin_registered(self):
        from django.contrib import admin
        from core.admin import PushScheduleAdmin
        from core.models import PushSchedule
        self.assertIsInstance(admin.site._registry.get(PushSchedule),
                              PushScheduleAdmin)


class ReportPushPayloadTest(TestCase):
    """⑤ 周报/月报推送：生成入知识库 + 深链文章 + 同周期只生成一份"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')

    def test_weekly_payload_links_to_article(self):
        from core.push import build_payload
        payload = build_payload(self.user, 'weekly_report')
        self.assertIn('周报', payload['title'])
        self.assertIn('完成', payload['body'])
        # 深链到生成的文章，点推送能直接看全文
        self.assertIn('/knowledge/', payload['url'])

    def test_monthly_payload_links_to_article(self):
        from core.push import build_payload
        payload = build_payload(self.user, 'monthly_report')
        self.assertIn('月报', payload['title'])
        self.assertIn('/knowledge/', payload['url'])

    def test_same_period_reuses_article(self):
        """同周期重复触发不得生成重复文章（幂等的第二层）"""
        from core.push import build_report_push_payload
        p1 = build_report_push_payload(self.user, 'weekly')
        p2 = build_report_push_payload(self.user, 'weekly')
        self.assertEqual(p1['url'], p2['url'])
        self.assertEqual(
            Article.objects.filter(
                user=self.user, tags__name='report-weekly').count(), 1)
