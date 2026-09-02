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
from activities.models import Activity, Expense, RecurringActivity
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
from core.models import Reminder, check_due_reminders
from core.suggestions import generate_daily_plan
from core.layout_asserts import assert_desktop_two_columns, code_only
from core.report_generator import (collect_report_data, generate_report,
                                   save_report_to_knowledge, _fallback_report)


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
        self.assertEqual(triggered, 1)

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


class ReminderDoneStatusTest(TestCase):
    """L8：「已完成」写 done，与系统自动触发的 fired 分开

    两者曾共用 fired：点完「已完成」条目仍留在 Daily「提醒」区（该区按 trigger_at
    筛今日、按 fired 取数，状态没变就看不出差异），浮窗红点也分不了
    「提醒过了」与「用户做完了」。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def fired_reminder(self, content='带伞'):
        reminder = Reminder.objects.create(
            user=self.user, content=content,
            trigger_at=timezone.now() - timedelta(minutes=5))
        check_due_reminders(self.user)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, 'fired')
        return reminder

    def test_done_endpoint_writes_done_not_fired(self):
        reminder = self.fired_reminder()
        self.assertEqual(self.client.post(
            f'/reminders/{reminder.id}/done/').status_code, 302)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, 'done')

    def test_done_leaves_daily_pending_list(self):
        reminder = self.fired_reminder()
        self.assertIn('带伞', self.client.get(reverse('activities:daily')).content.decode())
        self.client.post(f'/reminders/{reminder.id}/done/')
        self.assertNotIn('带伞', self.client.get(reverse('activities:daily')).content.decode())

    def test_widget_badge_counts_today_fired_and_stops_at_done(self):
        from chat.context_processors import chat_widget
        request = RequestFactory().get('/')
        request.user = self.user
        reminder = self.fired_reminder()
        self.assertEqual(chat_widget(request)['pending_reminder_count'], 1)
        self.client.post(f'/reminders/{reminder.id}/done/')
        self.assertEqual(chat_widget(request)['pending_reminder_count'], 0)

    def test_complete_tool_treats_done_as_processed(self):
        from core.agent_registry import get_tool
        reminder = self.fired_reminder(content='办签证')
        self.client.post(f'/reminders/{reminder.id}/done/')
        result = get_tool('reminders.complete')['fn'](self.user, {'target': '办签证'})
        self.assertIn('之前已经处理过了', result['reply'])


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
        from django.core.cache import cache
        cache.clear()
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


class SuggestionsBudgetBatchQueryTest(TestCase):
    """规则 7 预算预警：批量聚合取费用，查询数固定（无 N+1）"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')

    def test_budget_warning_batch_aggregation(self):
        """多个带预算活动时仅 2 条查询（活动 1 条 + 费用聚合 1 条）"""
        from core.suggestions import _rule_budget_warning

        for i in range(3):
            activity = Activity.objects.create(
                user=self.user, name=f'批量聚合活动{i}',
                budget=Decimal('1000'), status='in_progress',
            )
            for amount in ('500', '350'):
                Expense.objects.create(
                    activity=activity, user=self.user,
                    amount=Decimal(amount), category='food',
                )

        with self.assertNumQueries(2):
            result = _rule_budget_warning(self.user, timezone.localdate())

        # 预警最多保留 2 条，金额来自聚合结果而非逐条查询
        self.assertEqual(len(result), 2)
        for s in result:
            self.assertIn('接近预算', s['text'])
            self.assertIn('已花费 ¥850', s['text'])


class SuggestionsHabitMissedTest(TestCase):
    """规则 10 习惯断签：daily 循环活动昨日实例未完成 → 提示未打卡"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)

    def test_missed_daily_habit(self):
        """昨日 daily 实例状态非 done → 提示没有打卡"""
        from core.suggestions import generate_suggestions
        from activities.models import RecurringActivity

        pattern = RecurringActivity.objects.create(
            user=self.user, name='晨跑', frequency='daily', is_active=True,
        )
        Activity.objects.create(
            user=self.user, name='晨跑',
            start_date=self.yesterday, end_date=self.yesterday,
            status='planned', recurring_source=pattern,
        )

        texts = [s['text'] for s in generate_suggestions(self.user)]
        self.assertTrue(
            any('晨跑' in t and '没有打卡' in t for t in texts),
            f'Expected habit-missed suggestion, got: {texts}',
        )

    def test_done_habit_not_suggested(self):
        """昨日实例已完成 → 不提示"""
        from core.suggestions import generate_suggestions
        from activities.models import RecurringActivity

        pattern = RecurringActivity.objects.create(
            user=self.user, name='晨跑', frequency='daily', is_active=True,
        )
        Activity.objects.create(
            user=self.user, name='晨跑',
            start_date=self.yesterday, end_date=self.yesterday,
            status='done', recurring_source=pattern,
        )

        texts = [s['text'] for s in generate_suggestions(self.user)]
        self.assertFalse(any('没有打卡' in t for t in texts), texts)

    def test_weekly_habit_not_suggested(self):
        """非 daily 频率的循环活动不参与断签检查"""
        from core.suggestions import generate_suggestions
        from activities.models import RecurringActivity

        pattern = RecurringActivity.objects.create(
            user=self.user, name='周例会', frequency='weekly',
            day_of_week=0, is_active=True,
        )
        Activity.objects.create(
            user=self.user, name='周例会',
            start_date=self.yesterday, end_date=self.yesterday,
            status='planned', recurring_source=pattern,
        )

        texts = [s['text'] for s in generate_suggestions(self.user)]
        self.assertFalse(any('没有打卡' in t for t in texts), texts)


class SuggestionsEndingSoonTest(TestCase):
    """规则 11 临期活动：end_date 距今 ≤3 天且状态非 done/cancelled"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()

    def test_ending_in_days(self):
        """2 天后到期 → 提示剩余天数"""
        from core.suggestions import generate_suggestions

        Activity.objects.create(
            user=self.user, name='临期测试', status='in_progress',
            start_date=self.today - timedelta(days=5),
            end_date=self.today + timedelta(days=2),
        )

        texts = [s['text'] for s in generate_suggestions(self.user)]
        self.assertTrue(
            any('临期测试' in t and '还有 2 天到期' in t for t in texts),
            f'Expected ending-soon suggestion, got: {texts}',
        )

    def test_ending_today(self):
        """今天到期 → 提示今天到期"""
        from core.suggestions import generate_suggestions

        Activity.objects.create(
            user=self.user, name='今日截止', status='in_progress',
            start_date=self.today - timedelta(days=1), end_date=self.today,
        )

        texts = [s['text'] for s in generate_suggestions(self.user)]
        self.assertTrue(any('今日截止' in t and '今天到期' in t for t in texts), texts)

    def test_done_cancelled_and_far_dates_excluded(self):
        """done/cancelled 及超过 3 天的活动不提示"""
        from core.suggestions import generate_suggestions

        for status in ('done', 'cancelled'):
            Activity.objects.create(
                user=self.user, name=f'{status}活动', status=status,
                start_date=self.today, end_date=self.today + timedelta(days=1),
            )
        Activity.objects.create(
            user=self.user, name='远期活动', status='in_progress',
            start_date=self.today, end_date=self.today + timedelta(days=10),
        )

        texts = [s['text'] for s in generate_suggestions(self.user)]
        self.assertFalse(any('到期' in t for t in texts), texts)


class SuggestionsTruncationTest(TestCase):
    """建议总数超过上限时截断为 5 条"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')

    def test_max_suggestions(self):
        """多规则同时命中时最多返回 6 条"""
        from core.suggestions import generate_suggestions
        today = timezone.localdate()

        # 规则 1：明天有活动开始（1 条）
        Activity.objects.create(
            user=self.user, name='明日开始', status='planned',
            start_date=today + timedelta(days=1),
        )
        # 规则 4：计划中无日期（1 条）
        Activity.objects.create(
            user=self.user, name='没有日期', status='planned',
        )
        # 规则 6：今日无消费（1 条，费用未设 paid_at）
        # 规则 7：3 个超预算活动 → 2 条
        for i in range(3):
            activity = Activity.objects.create(
                user=self.user, name=f'超预算{i}', status='in_progress',
                start_date=today, budget=Decimal('100'),
            )
            Expense.objects.create(
                activity=activity, user=self.user, amount=Decimal('200'),
            )
        # 规则 8：2 个即将到期提醒 → 2 条（共 7 条）
        for i in range(2):
            Reminder.objects.create(
                user=self.user, content=f'提醒{i}',
                trigger_at=timezone.now() + timedelta(minutes=30),
            )

        suggestions = generate_suggestions(self.user)
        self.assertEqual(len(suggestions), 6)


class SuggestionsCacheTest(TestCase):
    """建议结果按用户缓存，数据源变更时信号失效"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')

    def test_cache_hit_and_signal_invalidation(self):
        """首次生成后写入缓存；新建活动触发信号清除缓存"""
        from django.core.cache import cache
        from core.suggestions import generate_suggestions, _cache_key

        first = generate_suggestions(self.user)
        self.assertIsNotNone(cache.get(_cache_key(self.user.id)))

        # 命中缓存：修改数据前二次调用返回同一对象内容且缓存仍在
        self.assertEqual(generate_suggestions(self.user), first)

        # 数据源变更 → post_save 信号清除缓存
        Activity.objects.create(user=self.user, name='新活动', status='planned')
        self.assertIsNone(cache.get(_cache_key(self.user.id)))


class SuggestionDismissReadTest(TestCase):
    """建议关闭/已读端点测试"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def test_dismiss_suggestion(self):
        """关闭建议后不再出现"""
        from core.models import SuggestionState
        from core.suggestions import generate_suggestions

        # 先创建一条建议数据
        activity = Activity.objects.create(
            user=self.user, name='测试活动',
            budget=Decimal('1000'), status='in_progress',
        )
        Expense.objects.create(
            activity=activity, user=self.user,
            amount=Decimal('900'), category='food',
        )

        suggestions = generate_suggestions(self.user)
        budget_suggestions = [s for s in suggestions if s['key'].startswith('budget:')]
        self.assertTrue(len(budget_suggestions) > 0)
        key = budget_suggestions[0]['key']

        # 关闭
        response = self.client.post(
            '/suggestions/dismiss/',
            data={'key': key},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])

        # 验证 SuggestionState 记录
        state = SuggestionState.objects.get(user=self.user, fingerprint=key)
        self.assertEqual(state.action, 'dismissed')

        # 重新生成建议，该条不再出现
        suggestions = generate_suggestions(self.user)
        keys = [s['key'] for s in suggestions]
        self.assertNotIn(key, keys)

    def test_read_suggestion(self):
        """标记已读"""
        from core.models import SuggestionState

        response = self.client.post(
            '/suggestions/read/',
            data={'key': 'test_key:1'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        state = SuggestionState.objects.get(user=self.user, fingerprint='test_key:1')
        self.assertEqual(state.action, 'read')

    def test_read_does_not_override_dismiss(self):
        """已读不覆盖已关闭"""
        from core.models import SuggestionState

        SuggestionState.objects.create(
            user=self.user, fingerprint='test_key:2', action='dismissed'
        )
        self.client.post(
            '/suggestions/read/',
            data={'key': 'test_key:2'},
            content_type='application/json',
        )
        state = SuggestionState.objects.get(user=self.user, fingerprint='test_key:2')
        self.assertEqual(state.action, 'dismissed')

    def test_dismiss_invalid_key(self):
        """无效 key 返回 400"""
        response = self.client.post(
            '/suggestions/dismiss/',
            data={'key': ''},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_dismiss_requires_login(self):
        """未登录返回重定向"""
        self.client.logout()
        response = self.client.post(
            '/suggestions/dismiss/',
            data={'key': 'x'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 302)


class SuggestionsNewRulesTest(TestCase):
    """新增规则测试"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()

    def test_expense_anomaly_triggers(self):
        """单日消费显著高于日均时触发异常提醒"""
        from core.suggestions import _rule_expense_anomaly

        # 近 30 天每天消费 20 元
        for i in range(1, 20):
            activity = Activity.objects.create(
                user=self.user, name=f'活动{i}', status='done',
                start_date=self.today - timedelta(days=i),
                end_date=self.today - timedelta(days=i),
            )
            Expense.objects.create(
                activity=activity, user=self.user,
                amount=Decimal('20'), category='food',
                paid_at=self.today - timedelta(days=i),
            )

        # 今天消费 200 元（远超日均 20 * 3 = 60）
        today_activity = Activity.objects.create(
            user=self.user, name='今日活动', status='done',
            start_date=self.today, end_date=self.today,
        )
        Expense.objects.create(
            activity=today_activity, user=self.user,
            amount=Decimal('200'), category='shopping',
            paid_at=self.today,
        )

        result = _rule_expense_anomaly(self.user, self.today)
        self.assertIsNotNone(result)
        self.assertIn('明显高于', result['text'])
        self.assertEqual(result['icon'], 'expense')

    def test_expense_anomaly_not_triggered(self):
        """正常消费不触发"""
        from core.suggestions import _rule_expense_anomaly

        activity = Activity.objects.create(
            user=self.user, name='普通活动', status='done',
            start_date=self.today, end_date=self.today,
        )
        Expense.objects.create(
            activity=activity, user=self.user,
            amount=Decimal('10'), category='food',
            paid_at=self.today,
        )

        result = _rule_expense_anomaly(self.user, self.today)
        self.assertIsNone(result)

    def test_habit_streak_positive(self):
        """连续打卡 ≥3 天时正向激励"""
        from core.suggestions import _rule_habit_streak
        from activities.models import RecurringActivity

        pattern = RecurringActivity.objects.create(
            user=self.user, name='冥想', frequency='daily', is_active=True,
        )
        # 连续 5 天打卡
        for i in range(1, 6):
            Activity.objects.create(
                user=self.user, name='冥想',
                start_date=self.today - timedelta(days=i),
                end_date=self.today - timedelta(days=i),
                status='done', recurring_source=pattern,
            )

        result = _rule_habit_streak(self.user, self.today)
        self.assertIsNotNone(result)
        self.assertTrue(len(result) > 0)
        self.assertIn('连续打卡 5 天', result[0]['text'])

    def test_subtask_progress_triggers(self):
        """子任务完成 ≥80% 时鼓励"""
        from core.suggestions import _rule_subtask_progress

        parent = Activity.objects.create(
            user=self.user, name='大项目', status='in_progress',
        )
        # 5 个子任务，4 个完成
        for i in range(5):
            Activity.objects.create(
                user=self.user, name=f'子任务{i}',
                parent=parent,
                status='done' if i < 4 else 'planned',
            )

        result = _rule_subtask_progress(self.user, self.today)
        self.assertIsNotNone(result)
        self.assertTrue(len(result) > 0)
        self.assertIn('只剩 1 个子任务', result[0]['text'])

    def test_goal_progress_no_goals(self):
        """无目标记忆时不触发"""
        from core.suggestions import _rule_goal_progress

        result = _rule_goal_progress(self.user, self.today)
        self.assertIsNone(result)

    def test_goal_progress_with_matching_activity(self):
        """有目标且有匹配活动时正向提示"""
        from core.suggestions import _rule_goal_progress
        from memory.models import Memory

        Memory.objects.create(
            user=self.user, content='学好英语',
            category='goal', importance=8,
        )
        Activity.objects.create(
            user=self.user, name='英语学习计划',
            status='in_progress',
            start_date=self.today,
        )

        result = _rule_goal_progress(self.user, self.today)
        self.assertIsNotNone(result)
        self.assertTrue(len(result) > 0)
        self.assertIn('进行中', result[0]['text'])


class ParseInsightsTest(TestCase):
    """AI 洞察 JSON 解析校验"""
    def test_valid_json(self):
        from core.management.commands.generate_daily_insights import parse_insights
        raw = '[{"text": "测试洞察", "icon": "plan", "action": {"label": "查看", "url": "/activities/"}}]'
        result = parse_insights(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['text'], '测试洞察')

    def test_invalid_json(self):
        from core.management.commands.generate_daily_insights import parse_insights
        self.assertEqual(parse_insights('not json'), [])
        self.assertEqual(parse_insights(''), [])
        self.assertEqual(parse_insights(None), [])

    def test_url_whitelist_filtering(self):
        from core.management.commands.generate_daily_insights import parse_insights
        raw = '[{"text": "测试", "icon": "plan", "action": {"label": "查看", "url": "https://evil.com"}}]'
        result = parse_insights(raw)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]['action'])

    def test_max_two_insights(self):
        from core.management.commands.generate_daily_insights import parse_insights
        raw = '[{"text": "1", "icon": "plan", "action": null}, {"text": "2", "icon": "goal", "action": null}, {"text": "3", "icon": "habit", "action": null}]'
        result = parse_insights(raw)
        self.assertEqual(len(result), 2)

    def test_markdown_fence_stripped(self):
        from core.management.commands.generate_daily_insights import parse_insights
        raw = '```json\n[{"text": "测试", "icon": "plan", "action": null}]\n```'
        result = parse_insights(raw)
        self.assertEqual(len(result), 1)


class DailyInsightMergeTest(TestCase):
    """AI 洞察合并到建议列表"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')

    def test_insights_merged_at_top(self):
        """DailyInsight 置顶，总上限 6"""
        from core.models import DailyInsight
        from core.suggestions import generate_suggestions

        today = timezone.localdate()
        DailyInsight.objects.create(
            user=self.user,
            insight_date=today,
            insights=[
                {'text': 'AI 洞察 1', 'icon': 'goal', 'key': 'ai:test:0', 'action': None, 'source': 'ai'},
                {'text': 'AI 洞察 2', 'icon': 'habit', 'key': 'ai:test:1', 'action': None, 'source': 'ai'},
            ],
            status='ready',
        )

        suggestions = generate_suggestions(self.user)
        # AI 洞察应在最前
        if suggestions:
            self.assertEqual(suggestions[0]['source'], 'ai')
            self.assertEqual(suggestions[0]['text'], 'AI 洞察 1')

    def test_dismissed_insight_hidden(self):
        """已关闭的洞察不再显示"""
        from core.models import DailyInsight, SuggestionState
        from core.suggestions import generate_suggestions

        today = timezone.localdate()
        DailyInsight.objects.create(
            user=self.user,
            insight_date=today,
            insights=[
                {'text': 'AI 洞察', 'icon': 'goal', 'key': 'ai:test:0', 'action': None, 'source': 'ai'},
            ],
            status='ready',
        )
        SuggestionState.objects.create(
            user=self.user, fingerprint='ai:test:0', action='dismissed'
        )

        suggestions = generate_suggestions(self.user)
        ai_keys = [s['key'] for s in suggestions if s.get('source') == 'ai']
        self.assertNotIn('ai:test:0', ai_keys)


class ComputeHabitStreaksTest(TestCase):
    """compute_habit_streaks 公共函数"""
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()

    def test_streak_calculation(self):
        from core.suggestions import compute_habit_streaks
        from activities.models import RecurringActivity

        pattern = RecurringActivity.objects.create(
            user=self.user, name='跑步', frequency='daily', is_active=True,
        )
        # 连续 3 天打卡（昨天、前天、大前天）
        for i in range(1, 4):
            Activity.objects.create(
                user=self.user, name='跑步',
                start_date=self.today - timedelta(days=i),
                status='done', recurring_source=pattern,
            )
        # 第 4 天未完成（断签）
        Activity.objects.create(
            user=self.user, name='跑步',
            start_date=self.today - timedelta(days=4),
            status='planned', recurring_source=pattern,
        )

        streaks = compute_habit_streaks(self.user, self.today)
        self.assertEqual(len(streaks), 1)
        self.assertEqual(streaks[0]['streak'], 3)
        self.assertEqual(streaks[0]['name'], '跑步')

    def test_no_active_recurring(self):
        from core.suggestions import compute_habit_streaks
        streaks = compute_habit_streaks(self.user, self.today)
        self.assertEqual(streaks, [])


class SuggestionActionProtocolTest(TestCase):
    """建议可操作增强：规则 action 协议（tool/post/link）+ followup"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()

    def _find(self, suggestions, key_prefix):
        return next((s for s in suggestions if s['key'].startswith(key_prefix)), None)

    def test_habit_missed_tool_action(self):
        """规则 10：补打卡→合法 tool action，params 携精确 activity_id"""
        from core.suggestions import generate_suggestions, SUGGESTION_TOOLS
        from activities.models import RecurringActivity

        pattern = RecurringActivity.objects.create(
            user=self.user, name='晨跑', frequency='daily', is_active=True)
        a = Activity.objects.create(
            user=self.user, name='晨跑',
            start_date=self.today - timedelta(days=1),
            end_date=self.today - timedelta(days=1),
            status='planned', recurring_source=pattern)

        s = self._find(generate_suggestions(self.user), 'habit_missed:')
        self.assertIsNotNone(s)
        self.assertTrue(s['followup'])
        action = s['action']
        self.assertEqual(action['kind'], 'tool')
        self.assertIn(action['tool'], SUGGESTION_TOOLS)
        self.assertEqual(action['params'], {'activity_id': a.id, 'status': 'done'})
        # params 全部为标量
        self.assertTrue(all(isinstance(v, (str, int, float, bool))
                            for v in action['params'].values()))

    def test_stale_planned_confirm_action(self):
        """规则 5：批量取消→tool action 需确认，target_ids 为整数列表"""
        from core.suggestions import generate_suggestions

        stale = Activity.objects.create(
            user=self.user, name='陈旧计划', status='planned',
            start_date=self.today - timedelta(days=40))
        Activity.objects.filter(id=stale.id).update(
            updated_at=timezone.now() - timedelta(days=31))

        s = self._find(generate_suggestions(self.user), 'stale_planned:')
        self.assertIsNotNone(s)
        action = s['action']
        self.assertEqual(action['kind'], 'tool')
        self.assertEqual(action['tool'], 'activities.batch_status')
        self.assertTrue(action['confirm'])
        self.assertTrue(action['summary'])
        self.assertEqual(action['params']['target_ids'], [stale.id])

    def test_today_expense_post_action(self):
        """规则 6：记一笔→post action 打开快记浮层 expense Tab"""
        from core.suggestions import generate_suggestions

        s = self._find(generate_suggestions(self.user), 'today_expense:')
        self.assertIsNotNone(s)
        self.assertEqual(s['action']['kind'], 'post')
        self.assertEqual(s['action']['panel'], 'expense')
        self.assertTrue(s['followup'])

    def test_all_suggestions_have_followup(self):
        """全量规则输出：每条建议 followup 非空，action kind 合法"""
        from core.suggestions import generate_suggestions, SUGGESTION_TOOLS
        from activities.models import RecurringActivity

        Activity.objects.create(user=self.user, name='陈旧计划', status='planned',
                                start_date=self.today - timedelta(days=40))
        Activity.objects.filter(name='陈旧计划').update(
            updated_at=timezone.now() - timedelta(days=31))
        RecurringActivity.objects.create(
            user=self.user, name='晨跑', frequency='daily', is_active=True)
        Activity.objects.create(
            user=self.user, name='晨跑',
            start_date=self.today - timedelta(days=1),
            end_date=self.today - timedelta(days=1),
            status='planned')

        for s in generate_suggestions(self.user):
            self.assertTrue(s['followup'], f"{s['key']} 缺 followup")
            action = s['action']
            if not action:
                continue
            kind = action.get('kind', 'link')  # 无 kind 视为 link（向后兼容）
            self.assertIn(kind, ('link', 'tool', 'post'))
            if kind == 'tool':
                self.assertIn(action['tool'], SUGGESTION_TOOLS)
                self.assertIsInstance(action['params'], dict)

    def test_link_action_backward_compatible(self):
        """现有 link action 结构不变（label + url）"""
        from core.suggestions import _normalize
        from datetime import date

        s = _normalize({'text': '测试', 'icon': 'plan',
                        'action': {'label': '查看', 'url': '/activities/'}},
                       'test_rule', date(2026, 1, 1))
        self.assertEqual(s['action'], {'label': '查看', 'url': '/activities/'})
        self.assertTrue(s['followup'])


class ParseInsightsActionTest(TestCase):
    """AI 洞察协议：tool action 白名单校验 + followup 兜底"""

    def test_valid_tool_action_passes(self):
        from core.management.commands.generate_daily_insights import parse_insights
        raw = ('[{"text": "晨跑断签了", "icon": "habit", '
               '"followup": "怎么恢复习惯", '
               '"action": {"kind": "tool", "label": "补打卡", '
               '"tool": "activities.set_status", "params": {"target": "晨跑", "status": "done"}}}]')
        result = parse_insights(raw)
        self.assertEqual(len(result), 1)
        action = result[0]['action']
        self.assertEqual(action['kind'], 'tool')
        self.assertEqual(action['tool'], 'activities.set_status')
        self.assertFalse(action['confirm'])  # set_status 白名单声明免确认
        self.assertEqual(result[0]['followup'], '怎么恢复习惯')

    def test_non_whitelist_tool_downgraded(self):
        from core.management.commands.generate_daily_insights import parse_insights
        raw = ('[{"text": "测试", "icon": "plan", '
               '"action": {"kind": "tool", "label": "删掉", '
               '"tool": "activities.delete", "params": {"target": "x"}}}]')
        result = parse_insights(raw)
        self.assertIsNone(result[0]['action'])

    def test_nested_params_downgraded(self):
        """params 携非标量值（嵌套结构）→ 降级 None"""
        from core.management.commands.generate_daily_insights import parse_insights
        raw = ('[{"text": "测试", "icon": "plan", '
               '"action": {"kind": "tool", "label": "打卡", '
               '"tool": "activities.set_status", "params": {"evil": {"a": 1}}}}]')
        result = parse_insights(raw)
        self.assertIsNone(result[0]['action'])

    def test_missing_followup_fallback(self):
        from core.management.commands.generate_daily_insights import parse_insights
        raw = '[{"text": "一条洞察", "icon": "plan", "action": null}]'
        result = parse_insights(raw)
        self.assertTrue(result[0]['followup'])
        self.assertIn('一条洞察', result[0]['followup'])


class SuggestionToolRunEndpointTest(TestCase):
    """建议工具执行端点：白名单 / 两步确认 / 标记已读 / 容错"""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user('testuser', password='test')
        self.client.login(username='testuser', password='test')
        self.url = '/suggestions/run-tool/'

    def _post(self, payload):
        import json
        return self.client.post(self.url, json.dumps(payload),
                                content_type='application/json')

    def test_requires_login(self):
        self.client.logout()
        resp = self._post({'tool': 'activities.set_status', 'params': {}})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp.url)

    def test_non_whitelist_tool_rejected(self):
        resp = self._post({'tool': 'activities.delete', 'params': {'target': 'x'}})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data['ok'])

    def test_invalid_params_rejected(self):
        """嵌套结构 params 拒绝"""
        resp = self._post({'tool': 'activities.set_status',
                           'params': {'evil': {'a': 1}}})
        self.assertEqual(resp.status_code, 400)

    def test_direct_tool_executes_and_marks_read(self):
        """免确认工具单次执行成功 → 状态变更 + 建议标记已读"""
        from core.models import SuggestionState

        a = Activity.objects.create(user=self.user, name='晨跑', status='planned')
        resp = self._post({'key': 'habit_missed:1', 'tool': 'activities.set_status',
                           'params': {'activity_id': a.id, 'status': 'done'}})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertIn('晨跑', data['reply'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'done')
        self.assertTrue(SuggestionState.objects.filter(
            user=self.user, fingerprint='habit_missed:1', action='read').exists())

    def test_confirm_two_step_flow(self):
        """需确认工具：首次 need_confirm+token → 带 token 二次执行；错误 token 拒绝"""
        from core.agent_registry import make_action_token

        a = Activity.objects.create(user=self.user, name='陈旧计划', status='planned')
        payload = {'key': 'stale_planned:x', 'tool': 'activities.batch_status',
                   'params': {'status': 'cancelled', 'target_ids': [a.id]},
                   'summary': '将取消 1 个活动'}

        # 第一步：无 token → 要求确认
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get('need_confirm'))
        self.assertFalse(data['ok'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'planned')  # 未执行

        # 错误 token → 400
        bad = dict(payload, confirm_token='0' * 32)
        resp = self._post(bad)
        self.assertEqual(resp.status_code, 400)
        a.refresh_from_db()
        self.assertEqual(a.status, 'planned')

        # 正确 token → 执行成功（target_ids 直达 apply）
        token = make_action_token(self.user, 'stale_planned:x', 'confirm')
        resp = self._post(dict(payload, confirm_token=token))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'cancelled')

    def test_tool_error_returns_ok_false(self):
        """工具抛 ToolError → 200 + ok=False，不抛 500"""
        resp = self._post({'key': 'k', 'tool': 'activities.set_status',
                           'params': {'activity_id': 999999, 'status': 'done'}})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data['ok'])
        self.assertTrue(data['error'])

    def test_invalid_body(self):
        resp = self.client.post(self.url, 'not json', content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class DailyPlanTest(TestCase):
    """Daily 页顶部区数据：只保留下方活动卡片没覆盖的信息，不重复列今日活动"""

    def setUp(self):
        self.user = User.objects.create_user('planuser', password='test')
        self.client.login(username='planuser', password='test')
        self.today = timezone.localdate()
        # 固定"当前时刻"为当天中午，绕开早间/晚间（hour < 18）时段互斥逻辑
        self.noon = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)

    def _span_today_activity(self):
        return Activity.objects.create(
            user=self.user, name='桐庐周末游', status='in_progress',
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=1),
        )

    def test_plan_no_longer_lists_today_activities(self):
        """跨今天的活动不再出现在 plan 里（改由「今日进行中」卡片承载）"""
        self._span_today_activity()
        plan = generate_daily_plan(self.user)
        self.assertNotIn('due_today', plan)
        self.assertTrue(plan['is_empty'])

    def test_daily_page_drops_due_group_and_renames_section(self):
        """顶部区标题改为「打卡与提醒」，不再渲染「今日到期」分组"""
        self._span_today_activity()
        with patch('django.utils.timezone.localtime', return_value=self.noon):
            html = self.client.get(reverse('activities:daily')).content.decode()
        self.assertIn('打卡与提醒', html)
        self.assertNotIn('今日到期', html)

    def test_daily_page_empty_plan_text(self):
        """三组全空时给出对应空状态文案"""
        with patch('django.utils.timezone.localtime', return_value=self.noon):
            html = self.client.get(reverse('activities:daily')).content.decode()
        self.assertIn('今天没有要打卡的习惯', html)

    def test_habits_group_lists_today_instance_only(self):
        """习惯只列今日实例，不抓未来实例"""
        habit = RecurringActivity.objects.create(
            user=self.user, name='晨跑', frequency='daily')
        Activity.objects.create(
            user=self.user, name='晨跑', start_date=self.today, recurring_source=habit)
        Activity.objects.create(
            user=self.user, name='晨跑', start_date=self.today + timedelta(days=1),
            recurring_source=habit)
        plan = generate_daily_plan(self.user)
        self.assertEqual([a.name for a in plan['habits']], ['晨跑'])
        self.assertFalse(plan['is_empty'])

    def test_subtask_groups_grouped_by_parent(self):
        """未完成子任务按父活动分组，已完成的不列入"""
        parent = Activity.objects.create(user=self.user, name='桐庐周末游')
        Activity.objects.create(user=self.user, name='门票', parent=parent,
                               start_date=self.today)
        Activity.objects.create(user=self.user, name='高铁', parent=parent,
                               status='in_progress')
        Activity.objects.create(user=self.user, name='已订完的酒店', parent=parent,
                               status='done')
        plan = generate_daily_plan(self.user)
        self.assertEqual(len(plan['subtask_groups']), 1)
        group = plan['subtask_groups'][0]
        self.assertEqual(group['parent'].name, '桐庐周末游')
        self.assertEqual({c.name for c in group['children']}, {'门票', '高铁'})

    def test_reminders_only_pending_before_tomorrow(self):
        """只取今天内待触发（pending）的提醒，已触发或时间还在几天外的都排除"""
        # 基准用当天中午而非“现在”：否则晚间跑用例时 now+2h 会跨到明天，导致随机失败
        Reminder.objects.create(user=self.user, content='下午开会',
                                trigger_at=self.noon + timedelta(hours=2))
        Reminder.objects.create(user=self.user, content='下周提交',
                                trigger_at=self.noon + timedelta(days=7))
        Reminder.objects.create(user=self.user, content='已触发过',
                                trigger_at=self.noon - timedelta(hours=1), status='fired')
        plan = generate_daily_plan(self.user)
        self.assertEqual([r.content for r in plan['reminders']], ['下午开会'])


class SuggestionDeepLinkTest(TestCase):
    """建议深链：action.url 带的查询参数必须真被目标视图消费

    踩过一次：URL 拼 ?start_date=…，而列表页只认 date_from/date_to，
    条件被 filter_activities 静默丢弃 → 点「查看」看到的是全量 planned 列表。
    """

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.today = timezone.localdate()

    def test_starting_tomorrow_link_only_lists_tomorrow(self):
        """「明天有活动即将开始」的跳转链只能列出明天的活动"""
        from core.suggestions import generate_suggestions

        tomorrow = self.today + timedelta(days=1)
        Activity.objects.create(user=self.user, name='明天开会',
                               status='planned', start_date=tomorrow)
        Activity.objects.create(user=self.user, name='下周出发',
                               status='planned', start_date=tomorrow + timedelta(days=5))

        suggestions = generate_suggestions(self.user)
        link = next((s['action']['url'] for s in suggestions
                     if str(s.get('key', '')).startswith('starting_tomorrow:')), None)
        self.assertIsNotNone(link, f'未生成明日开始建议: {[s["text"] for s in suggestions]}')

        ctx = self.client.get(link).context
        self.assertEqual([a.name for a in ctx['activities']], ['明天开会'])


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
        a.tags.add('运动')
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
        'activities/daily.html', 'activities/activity_detail.html',
        'core/dashboard.html', 'core/weekly_report.html',
        'activities/expense_report.html', 'activities/template_list.html',
        'activities/recurring_list.html', 'chat/conversation_list.html',
        'knowledge/article_list.html', 'knowledge/article_detail.html',
        'notes/note_list.html', 'memory/memory_list.html',
    ]
    # 刻意保持单列：宽表/宽网格、单一任务页、等分双组页
    SINGLE_COLUMN = {
        'activities/activity_list.html': '宽表格 min-w-[760px]，吃满整宽才放得下',
        'activities/activity_calendar.html': '月/周视图是 7 列网格，压进左列每格不足 123px',
        'activities/next_actions.html': '两组等权重内容，用等分 md:grid-cols-2 而非主+辅',
        'activities/activity_form.html': '纯表单编辑页，输入宽度就是舒适宽度',
        'knowledge/article_form.html': '纯表单编辑页',
        'chat/conversation_detail.html': '消息流是单一线性时间轴，右列无天然内容',
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

    CORE_TAGS = ('staticv', 'ai_markdown', 'json_url')

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
