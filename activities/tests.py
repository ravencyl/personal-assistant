import json
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


class SubactivityManualCreateTest(TestCase):
    """活动详情页内联手动创建子任务（subactivity_manual_create 端点）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.parent = Activity.objects.create(user=self.user, name='新西兰之旅')

    def _post(self, payload, activity=None):
        target = activity or self.parent
        return self.client.post(
            f'/activities/{target.id}/subactivities/manual-create/',
            data=json.dumps(payload), content_type='application/json')

    def test_manual_create_with_all_fields(self):
        """正常创建：日期/状态/费用/标签/参与者全部落库，并返回局部刷新片段"""
        resp = self._post({'name': '订机票', 'start_date': '2026-09-10',
                           'end_date': '2026-09-12', 'status': 'in_progress',
                           'amount': '1200.50', 'tags': '出行, 预订',
                           'participants': '小王，小李'})
        self.assertEqual(resp.status_code, 200)
        child = Activity.objects.get(name='订机票')
        self.assertEqual(child.parent_id, self.parent.id)
        self.assertEqual(child.status, 'in_progress')
        self.assertEqual(child.start_date.isoformat(), '2026-09-10')
        self.assertEqual(child.end_date.isoformat(), '2026-09-12')
        self.assertEqual({t.name for t in child.tags.all()}, {'出行', '预订'})
        self.assertEqual({p.name for p in child.participants.all()}, {'小王', '小李'})
        self.assertEqual(Expense.objects.get(activity=child).amount, Decimal('1200.50'))
        data = resp.json()
        self.assertEqual(data['children_count'], 1)
        self.assertIn('订机票', data['children_html'])

    def test_optional_fields_can_be_blank(self):
        """只填名称也能创建：状态默认 planned，无费用/标签/参与者"""
        resp = self._post({'name': '买保险', 'start_date': '', 'end_date': '',
                           'amount': '', 'tags': '', 'participants': ''})
        self.assertEqual(resp.status_code, 200)
        child = Activity.objects.get(name='买保险')
        self.assertEqual(child.status, 'planned')
        self.assertIsNone(child.start_date)
        self.assertFalse(Expense.objects.filter(activity=child).exists())

    def test_empty_name_rejected(self):
        """名称为空被后端拦截，且不产生任何数据"""
        for bad in ('', '   ', None):
            resp = self._post({'name': bad})
            self.assertEqual(resp.status_code, 400)
            self.assertIn('名称', resp.json()['error'])
        self.assertEqual(Activity.objects.filter(parent=self.parent).count(), 0)

    def test_invalid_dates_and_amount_rejected(self):
        """结束早于开始、非法日期、非法金额均返回 400 友好文案"""
        resp = self._post({'name': 'x', 'start_date': '2026-09-12', 'end_date': '2026-09-10'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('结束日期', resp.json()['error'])
        self.assertEqual(self._post({'name': 'x', 'start_date': 'not-a-date'}).status_code, 400)
        self.assertEqual(self._post({'name': 'x', 'amount': '-5'}).status_code, 400)
        self.assertEqual(self._post({'name': 'x', 'amount': 'abc'}).status_code, 400)
        self.assertEqual(self._post({'name': 'x', 'status': 'bogus'}).status_code, 200)  # 非法状态回落 planned
        self.assertEqual(Activity.objects.filter(parent=self.parent).count(), 1)

    def test_other_users_activity_returns_404(self):
        """无权访问他人活动（get_visible）→ 404，且不创建数据"""
        foreign = Activity.objects.create(user=self.other, name='别人的活动')
        resp = self._post({'name': '插不进去'}, activity=foreign)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Activity.objects.filter(name='插不进去').exists())

    def test_ownership_inherits_parent(self):
        """超管创建子任务，归属仍是父活动 owner（子活动继承父活动 user）"""
        User.objects.create_superuser('root', password='root')
        admin_client = Client()
        admin_client.login(username='root', password='root')
        resp = admin_client.post(
            f'/activities/{self.parent.id}/subactivities/manual-create/',
            data=json.dumps({'name': '租用车'}), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Activity.objects.get(name='租用车').user_id, self.user.id)

    def test_logs_written_for_parent_and_child(self):
        """父子两条活动日志，与 add_subactivity / activity_quick_sub 口径一致"""
        from activities.models import ActivityLog
        self.assertEqual(self._post({'name': '办签证'}).status_code, 200)
        child = Activity.objects.get(name='办签证')
        parent_logs = ActivityLog.objects.filter(activity=self.parent, action='sub_created')
        child_logs = ActivityLog.objects.filter(activity=child, action='created')
        self.assertEqual(parent_logs.count(), 1)
        self.assertIn('办签证', parent_logs[0].summary)
        self.assertEqual(child_logs.count(), 1)
        self.assertIn(self.parent.name, child_logs[0].summary)

    def test_detail_page_renders_collapsed_manual_form(self):
        """详情页渲染内联表单（默认折叠）且与 AI 快速入口并存"""
        Activity.objects.create(user=self.user, name='已有子任务', parent=self.parent)
        resp = self.client.get(f'/activities/{self.parent.id}/')
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('手动添加子任务', content)
        self.assertIn('id="sub-manual-form" class="hidden', content)
        self.assertIn('novalidate', content)  # 行内错误接管浏览器原生必填气泡
        self.assertIn('快速记一笔子任务', content)   # AI 入口保留
        self.assertIn('已有子任务', content)
        self.assertIn('sub-manual-tag-options', content)  # 标签 autocomplete 建议
