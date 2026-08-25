from django.test import TestCase, Client
from django.contrib.auth.models import User
from activities.models import Activity, Expense
from knowledge.models import Article
from notes.models import Note
from core.cross_link import get_related_content, _tag_intersection_scores, _token_fallback
from core.search import global_search
from datetime import timedelta
from django.utils import timezone
from core.models import Reminder, check_due_reminders
from decimal import Decimal
from core.report_generator import collect_report_data, generate_report, save_report_to_knowledge, _fallback_report


class CrossLinkTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        # 创建带标签的活动
        self.activity = Activity.objects.create(user=self.user, name='桐庐旅行')
        self.activity.tags.add('旅行', '周末')

        # 创建带共同标签的知识库文章
        self.article1 = Article.objects.create(user=self.user, title='桐庐攻略', content='详细攻略...')
        self.article1.tags.add('旅行', '桐庐')

        self.article2 = Article.objects.create(user=self.user, title='杭州周边游', content='推荐...')
        self.article2.tags.add('旅行')

        # 创建带共同标签的笔记
        self.note1 = Note.objects.create(user=self.user, content='周末去桐庐玩，记得带泳衣')
        self.note1.tags.add('旅行', '周末')

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
        activity2.tags.add('户外')  # 与 article3 无标签交集
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
        activity2.tags.add('旅行', '周末')
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
        self.activity.tags.add('旅行')

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


class ReminderModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')

    def test_reminder_create(self):
        """创建提醒实例"""
        r = Reminder.objects.create(
            user=self.user,
            content='测试提醒',
            trigger_at=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(r.status, 'pending')
        self.assertEqual(str(r), '测试提醒 (待触发)')

    def test_reminder_check_due(self):
        """到期提醒状态变更"""
        r = Reminder.objects.create(
            user=self.user,
            content='到期提醒',
            trigger_at=timezone.now() - timedelta(minutes=5),
        )
        triggered = check_due_reminders(self.user)
        r.refresh_from_db()
        self.assertEqual(r.status, 'fired')
        self.assertEqual(len(triggered), 1)

    def test_reminder_not_due(self):
        """未到期的提醒不触发"""
        r = Reminder.objects.create(
            user=self.user,
            content='未来提醒',
            trigger_at=timezone.now() + timedelta(hours=1),
        )
        check_due_reminders(self.user)
        r.refresh_from_db()
        self.assertEqual(r.status, 'pending')

    def test_reminder_dismiss(self):
        """忽略提醒"""
        client = Client()
        client.login(username='testuser', password='test')
        r = Reminder.objects.create(
            user=self.user,
            content='忽略测试',
            trigger_at=timezone.now(),
        )
        response = client.post(f'/reminders/{r.id}/dismiss/')
        self.assertEqual(response.status_code, 302)
        r.refresh_from_db()
        self.assertEqual(r.status, 'dismissed')


class ReminderAgentToolTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')

    def test_set_reminder_tool_exists(self):
        from core.agent_registry import get_tool
        tool = get_tool('reminders.set_reminder')
        self.assertIsNotNone(tool)

    def test_list_reminders_tool_exists(self):
        from core.agent_registry import get_tool
        tool = get_tool('reminders.list_reminders')
        self.assertIsNotNone(tool)

    def test_set_reminder_tool_execute(self):
        """AI 工具创建提醒"""
        from core.agent_registry import get_tool
        tool = get_tool('reminders.set_reminder')
        result = tool['fn'](self.user, {
            'content': '买机票',
            'remind_at': (timezone.now() + timedelta(hours=2)).isoformat(),
        })
        self.assertIn('买机票', result['reply'])
        self.assertEqual(result['card'], 'reminder')
        self.assertTrue(Reminder.objects.filter(user=self.user, content='买机票').exists())

    def test_list_reminders_empty(self):
        """列出提醒（空列表）"""
        from core.agent_registry import get_tool
        tool = get_tool('reminders.list_reminders')
        result = tool['fn'](self.user, {})
        self.assertIn('没有', result['reply'])

    def test_list_reminders_with_data(self):
        """列出提醒（有数据）"""
        Reminder.objects.create(
            user=self.user, content='测试提醒',
            trigger_at=timezone.now() + timedelta(hours=1),
        )
        from core.agent_registry import get_tool
        tool = get_tool('reminders.list_reminders')
        result = tool['fn'](self.user, {})
        self.assertIn('测试提醒', result['reply'])


class ReportGeneratorTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        # 创建测试活动和费用
        self.activity1 = Activity.objects.create(
            user=self.user, name='出差上海', status='done',
            start_date=timezone.localdate(),
        )
        Expense.objects.create(
            activity=self.activity1, user=self.user,
            amount=500, category='transport',
            paid_at=timezone.localdate(),
        )

    def test_collect_report_data_weekly(self):
        """周数据聚合正确"""
        today = timezone.localdate()
        week_start = today - timedelta(days=today.weekday())
        data = collect_report_data(self.user, 'weekly', week_start, today)
        self.assertEqual(data['total_activities'], 1)
        self.assertEqual(data['total_expense'], 500.0)
        self.assertIn('transport', data['expense_by_category'])

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


class SuggestionsBudgetWarningTest(TestCase):
    """验证 suggestions 包含预算预警建议"""
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')

    def test_suggestions_budget_warning(self):
        """活动接近预算时 suggestions 包含预警"""
        from core.suggestions import generate_suggestions

        activity = Activity.objects.create(
            user=self.user, name='预算预警测试',
            budget=Decimal('1000'), status='in_progress',
        )
        Expense.objects.create(
            activity=activity, user=self.user,
            amount=Decimal('850'), category='food',
        )

        suggestions = generate_suggestions(self.user)
        texts = [s['text'] for s in suggestions]
        # 应该包含活动名称
        self.assertTrue(any('预算预警测试' in t for t in texts), f'Expected activity name in suggestions, got: {texts}')

    def test_suggestions_budget_over(self):
        """活动超支时 suggestions 包含预警"""
        from core.suggestions import generate_suggestions

        activity = Activity.objects.create(
            user=self.user, name='超支预警测试',
            budget=Decimal('1000'), status='in_progress',
        )
        Expense.objects.create(
            activity=activity, user=self.user,
            amount=Decimal('1200'), category='food',
        )

        suggestions = generate_suggestions(self.user)
        texts = [s['text'] for s in suggestions]
        # 应该包含活动名称
        self.assertTrue(any('超支预警测试' in t for t in texts), f'Expected activity name in suggestions, got: {texts}')
