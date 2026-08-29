from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta
from django.db.models import Sum
from activities.models import Activity, Expense
from activities.utils import budget_status, get_daily_bucket, DAILY_BUCKET_NAME


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


class AddExpenseAutoTargetTest(TestCase):
    """记账工具 target 缺省时的归属链：日期重叠 → note 关键词 → 日常开支桶"""

    def setUp(self):
        from core.agent_registry import get_tool
        self.tool = get_tool('activities.add_expense')
        self.assertIsNotNone(self.tool)
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()

    def test_no_target_fallback_to_daily_bucket(self):
        """无 target 且无可归属活动时，费用记入「日常开支」归属桶"""
        result = self.tool['fn'](self.user, {'amount': 25, 'category': '餐饮', 'note': '午饭'})
        bucket = get_daily_bucket(self.user)
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity_id, bucket.id)
        self.assertEqual(bucket.name, DAILY_BUCKET_NAME)
        self.assertEqual(bucket.status, 'in_progress')
        self.assertIn('日常开支', result['reply'])
        self.assertTrue(result['changed'])

    def test_no_target_keyword_unique_match(self):
        """无 target 时按 note 关键词唯一命中进行中活动（活动日期不与今昨重叠）"""
        Activity.objects.create(
            user=self.user, name='出差上海', status='in_progress',
            start_date=self.today - timedelta(days=30),
            end_date=self.today - timedelta(days=25),
        )
        result = self.tool['fn'](self.user, {'amount': 30, 'category': '交通', 'note': '上海 打车 35'})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity.name, '出差上海')
        self.assertIn('出差上海', result['reply'])

    def test_with_target_original_path(self):
        """有 target 时走原匹配路径，行为不变"""
        activity = Activity.objects.create(user=self.user, name='周末露营')
        result = self.tool['fn'](self.user, {'target': '露营', 'amount': 120, 'category': '购物'})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity_id, activity.id)
        self.assertEqual(result['reply'], f'已为「周末露营」添加费用 ¥120.0（购物）')

    def test_no_target_date_overlap_unique(self):
        """无 target 时当日/昨日日期重叠的唯一进行中活动优先命中"""
        Activity.objects.create(
            user=self.user, name='桐庐旅行', status='in_progress',
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=1),
        )
        result = self.tool['fn'](self.user, {'amount': 66, 'category': '餐饮'})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity.name, '桐庐旅行')
        self.assertIn('桐庐旅行', result['reply'])

    def test_bucket_hidden_from_activity_list_page(self):
        """归属桶不出现在活动列表页，但费用统计口径包含桶内费用"""
        self.tool['fn'](self.user, {'amount': 10})
        client = Client()
        client.login(username='testuser', password='test')
        response = client.get('/activities/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(DAILY_BUCKET_NAME, response.content.decode())
        # 费用统计口径包含桶内费用（按用户聚合）
        total = Expense.objects.filter(user=self.user).aggregate(s=Sum('amount'))['s']
        self.assertEqual(total, Decimal('10'))


class ActivitySearchTest(TestCase):
    """活动搜索：跨标题/描述/参与者/标签关键词匹配（filter_activities + 列表页）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def _filter_names(self, **params):
        from activities.utils import filter_activities
        return set(filter_activities(self.user, params).values_list('name', flat=True))

    def test_keyword_matches_name_description_tag_participant(self):
        """同一关键词应跨名称/描述/标签/参与者四个字段命中"""
        from activities.models import Participant
        a1 = Activity.objects.create(user=self.user, name='团建策划')
        a2 = Activity.objects.create(user=self.user, name='会议', description='季度团建复盘')
        a3 = Activity.objects.create(user=self.user, name='爬山')
        a3.tags.add('团建活动')
        a4 = Activity.objects.create(user=self.user, name='晚餐')
        p = Participant.objects.create(user=self.user, name='团建达人小王')
        a4.participants.add(p)
        other = Activity.objects.create(user=self.user, name='无关活动')

        names = self._filter_names(keyword='团建')
        self.assertEqual(names, {'团建策划', '会议', '爬山', '晚餐'})
        self.assertNotIn(other.name, names)

    def test_keyword_case_insensitive_and_blank(self):
        """英文关键词大小写不敏感；空白关键词不过滤"""
        Activity.objects.create(user=self.user, name='Team Building 年度活动')
        self.assertEqual(self._filter_names(keyword='team'), {'Team Building 年度活动'})
        self.assertEqual(self._filter_names(keyword='  ')
                         , {'Team Building 年度活动'})

    def test_list_page_search_view_and_tree_ancestors(self):
        """列表页搜索：命中子活动保留祖先链展示，未命中活动隐藏，显示命中提示"""
        parent = Activity.objects.create(user=self.user, name='新西兰之旅')
        child = Activity.objects.create(user=self.user, name='订机票', parent=parent,
                                        description='新西兰航空')
        Activity.objects.create(user=self.user, name='本地跑步')

        response = self.client.get('/activities/?keyword=新西兰')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('订机票', content)      # 命中（描述）
        self.assertIn('新西兰之旅', content)  # 祖先链保留
        self.assertNotIn('本地跑步', content)
        self.assertIn('命中', content)          # 命中数提示

    def test_list_page_search_combines_with_status_filter(self):
        """搜索 + 状态筛选叠加，且关键词回填到搜索框"""
        Activity.objects.create(user=self.user, name='团建爬山', status='done')
        Activity.objects.create(user=self.user, name='团建聚餐', status='planned')

        response = self.client.get('/activities/?keyword=团建&status=done')
        content = response.content.decode()
        self.assertIn('团建爬山', content)
        self.assertNotIn('团建聚餐', content)
        self.assertIn('name="keyword" value="团建"', content)  # 搜索框回填

    def test_list_page_search_no_match(self):
        """无命中时不报错，展示空列表"""
        Activity.objects.create(user=self.user, name='存在活动')
        response = self.client.get('/activities/?keyword=完全不存在的关键词xyz')
        self.assertEqual(response.status_code, 200)
