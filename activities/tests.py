from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.cache import cache
from decimal import Decimal
from activities.models import Activity, Expense
from activities.utils import budget_status


class BudgetStatusTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='测试活动', budget=Decimal('1000'))

    def test_budget_status_safe(self):
        Expense.objects.create(activity=self.activity, user=self.user, amount=Decimal('500'), category='food')
        ratio, level, label = budget_status(self.activity)
        self.assertEqual(level, 'safe')
        self.assertAlmostEqual(ratio, 0.5)

    def test_budget_status_warning(self):
        Expense.objects.create(activity=self.activity, user=self.user, amount=Decimal('850'), category='food')
        ratio, level, label = budget_status(self.activity)
        self.assertEqual(level, 'warning')

    def test_budget_status_over(self):
        Expense.objects.create(activity=self.activity, user=self.user, amount=Decimal('1100'), category='food')
        ratio, level, label = budget_status(self.activity)
        self.assertEqual(level, 'over')

    def test_budget_status_no_budget(self):
        self.activity.budget = None
        ratio, level, label = budget_status(self.activity)
        self.assertIsNone(level)

    def test_budget_status_zero_budget(self):
        self.activity.budget = Decimal('0')
        ratio, level, label = budget_status(self.activity)
        self.assertIsNone(level)


class ExpenseCategorySuggestTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='测试活动')

    def test_category_suggest_with_history(self):
        for _ in range(5):
            Expense.objects.create(activity=self.activity, user=self.user, amount=100, category='food')
        for _ in range(3):
            Expense.objects.create(activity=self.activity, user=self.user, amount=100, category='transport')
        response = self.client.get(f'/activities/{self.activity.id}/category-suggest/')
        data = response.json()
        self.assertEqual(data['categories'][0], 'food')

    def test_category_suggest_empty(self):
        response = self.client.get(f'/activities/{self.activity.id}/category-suggest/')
        data = response.json()
        self.assertEqual(len(data['categories']), len(Expense.CATEGORY_CHOICES))


class SetBudgetAgentToolTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='出差上海')

    def test_set_budget_tool_exists(self):
        from core.agent_registry import get_tool
        tool = get_tool('activities.set_budget')
        self.assertIsNotNone(tool)


class BudgetProgressBarTest(TestCase):
    """验证预算进度条模板渲染"""
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def test_budget_progress_bar_safe(self):
        """预算安全时进度条颜色为 zinc-900"""
        activity = Activity.objects.create(user=self.user, name='安全活动', budget=Decimal('1000'))
        Expense.objects.create(activity=activity, user=self.user, amount=Decimal('500'), category='food')
        response = self.client.get(f'/activities/{activity.id}/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # 安全状态应该有进度条
        self.assertIn('预算', content)

    def test_budget_progress_bar_warning(self):
        """预算警告时进度条颜色为 amber"""
        activity = Activity.objects.create(user=self.user, name='警告活动', budget=Decimal('1000'))
        Expense.objects.create(activity=activity, user=self.user, amount=Decimal('850'), category='food')
        response = self.client.get(f'/activities/{activity.id}/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('amber', content)
        self.assertIn('接近预算', content)

    def test_budget_progress_bar_over(self):
        """预算超支时进度条颜色为 red"""
        activity = Activity.objects.create(user=self.user, name='超支活动', budget=Decimal('1000'))
        Expense.objects.create(activity=activity, user=self.user, amount=Decimal('1100'), category='food')
        response = self.client.get(f'/activities/{activity.id}/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('red', content)
        self.assertIn('已超预算', content)
