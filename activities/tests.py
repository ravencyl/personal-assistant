import json
import os
import re
import tempfile
from decimal import Decimal
from datetime import timedelta
from io import StringIO

from django.test import TestCase, Client, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone
from django.db.models import Sum

from activities.models import Activity, Attachment, Expense, Participant
from activities.utils import (budget_status, get_daily_bucket, DAILY_BUCKET_NAME,
                              resolve_participants)


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


class ResolveParticipantsTest(TestCase):
    """参与者解析：大小写不敏感匹配已有名单，自动识别路径不新建"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.yyx = Participant.objects.create(user=self.user, name='YYX', note='杨雨闲')

    def test_case_insensitive_match_without_create(self):
        matched, skipped, created = resolve_participants(self.user, ['yyx', ' Yyx ', '@YYX'])
        self.assertEqual(matched, [self.yyx])
        self.assertEqual((skipped, created), ([], []))
        self.assertEqual(Participant.objects.filter(user=self.user).count(), 1)

    def test_unknown_name_is_skipped(self):
        matched, skipped, created = resolve_participants(self.user, ['路人甲'])
        self.assertEqual(matched, [])
        self.assertEqual(skipped, ['路人甲'])
        self.assertEqual(created, [])
        self.assertEqual(Participant.objects.count(), 1)

    def test_create_missing_reuses_existing_spelling(self):
        matched, skipped, created = resolve_participants(
            self.user, ['yyx', '小李'], create_missing=True)
        self.assertEqual([p.name for p in matched], ['YYX', '小李'])
        self.assertEqual(created, ['小李'])
        self.assertEqual(skipped, [])
        self.assertFalse(Participant.objects.filter(name='yyx').exists())

    def test_blank_input(self):
        self.assertEqual(resolve_participants(self.user, []), ([], [], []))
        self.assertEqual(resolve_participants(self.user, None), ([], [], []))
        self.assertEqual(resolve_participants(self.user, ['  ', '']), ([], [], []))

    def test_other_users_participants_are_invisible(self):
        matched, skipped, _created = resolve_participants(self.other, ['YYX'])
        self.assertEqual(matched, [])
        self.assertEqual(skipped, ['YYX'])


class ParticipantAgentToolTest(TestCase):
    """AI 对话创建/修改活动：只填已有参与者，不自动新建联系人"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.yyx = Participant.objects.create(user=self.user, name='YYX')

    def test_create_tool_skips_unknown_participant(self):
        from core.agent_registry import get_tool
        result = get_tool('activities.create')['fn'](self.user, {
            'name': '和 yyx 吃饭', 'participants': ['yyx', '路人甲']})
        activity = Activity.objects.get(id=result['activity_ids'][0])
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['YYX'])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists())
        self.assertIn('路人甲', result['reply'])
        self.assertIn('未添加', result['reply'])

    def test_update_tool_does_not_clear_when_nothing_matches(self):
        activity = Activity.objects.create(user=self.user, name='周末游')
        activity.participants.set([self.yyx])
        from core.agent_registry import get_tool
        tool = get_tool('activities.update')

        preview = tool['fn'](self.user, {'target': '周末游', 'participants': ['路人甲']})
        self.assertIn('未添加', preview['reply'])

        applied = tool['apply'](self.user, {'target_id': activity.id, 'participants': ['路人甲']})
        activity.refresh_from_db()
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['YYX'])
        self.assertIn('未添加', applied['reply'])

    def test_update_tool_replaces_with_matched_only(self):
        activity = Activity.objects.create(user=self.user, name='周末游')
        li = Participant.objects.create(user=self.user, name='小李')
        activity.participants.set([li])
        from core.agent_registry import get_tool
        tool = get_tool('activities.update')
        tool['apply'](self.user, {'target_id': activity.id, 'participants': ['yyx']})
        activity.refresh_from_db()
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['YYX'])


class ParticipantQuickEndpointTest(TestCase):
    """快速创建 / 一句话子任务：未命中的参与者跳过并在响应 note 中说明"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.yyx = Participant.objects.create(user=self.user, name='YYX')
        self.parent = Activity.objects.create(user=self.user, name='新西兰之旅')

    def test_quick_create_skips_unknown(self):
        resp = self.client.post(
            '/activities/quick-create/',
            data=json.dumps({'name': '周末聚餐', 'participants': ['yyx', '路人甲']}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        activity = Activity.objects.get(name='周末聚餐')
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['YYX'])
        self.assertIn('路人甲', resp.json()['note'])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists())

    def test_quick_sub_skips_unknown(self):
        resp = self.client.post(
            f'/activities/{self.parent.id}/quick-sub/',
            data=json.dumps({'name': '订门票', 'participants': ['路人甲']}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        child = Activity.objects.get(name='订门票')
        self.assertEqual(child.participants.count(), 0)
        self.assertIn('路人甲', resp.json()['note'])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists())

    def test_manual_form_normalizes_case_before_creating(self):
        """内联手动表单：手输 yyx 归到已有 YYX，真正的新名字才新建"""
        resp = self.client.post(
            f'/activities/{self.parent.id}/subactivities/manual-create/',
            data=json.dumps({'name': '接机', 'participants': 'yyx, 小王'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        child = Activity.objects.get(name='接机')
        self.assertEqual({p.name for p in child.participants.all()}, {'YYX', '小王'})
        self.assertFalse(Participant.objects.filter(name='yyx').exists())
        self.assertIn('小王', resp.json()['note'])


class MergeParticipantsCommandTest(TestCase):
    """merge_participants：默认 dry-run，--apply 才合并"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.keep = Participant.objects.create(user=self.user, name='YYX', note='杨雨闲')
        self.dup = Participant.objects.create(user=self.user, name='yyx')
        self.activity = Activity.objects.create(user=self.user, name='桐庐周末游')
        self.activity.participants.set([self.dup])

    def _run(self, *args):
        out = StringIO()
        call_command('merge_participants', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        text = self._run()
        self.assertIn('保留「YYX」', text)
        self.assertIn('合并「yyx」', text)
        self.assertIn('dry-run', text)
        self.assertTrue(Participant.objects.filter(id=self.dup.id).exists())
        self.assertEqual(list(self.activity.participants.values_list('name', flat=True)), ['yyx'])

    def test_apply_merges_relations_and_deletes_dup(self):
        text = self._run('--apply')
        self.assertIn('已合并 1 条', text)
        self.assertFalse(Participant.objects.filter(id=self.dup.id).exists())
        self.assertEqual(list(self.activity.participants.values_list('name', flat=True)), ['YYX'])
        self.assertEqual(Participant.objects.filter(user=self.user).count(), 1)

    def test_user_filter_and_no_duplicates(self):
        with self.assertRaises(CommandError):
            call_command('merge_participants', '--user', 'nobody', stdout=StringIO())
        Participant.objects.filter(id=self.dup.id).delete()
        self.assertIn('无需处理', self._run())

    def test_map_merges_alias_with_explicit_target(self):
        """--map「Joe:Joe Yan」：写法不同的同人也能合并，活动关联迁移到保留名"""
        keep = Participant.objects.create(user=self.user, name='Joe Yan')
        alias = Participant.objects.create(user=self.user, name='Joe')
        activity = Activity.objects.create(user=self.user, name='周会')
        activity.participants.set([alias])

        self.assertIn('保留「Joe Yan」', self._run('--map', 'joe:Joe Yan'))
        text = self._run('--map', 'Joe:Joe Yan', '--apply')
        self.assertIn('已合并', text)
        self.assertFalse(Participant.objects.filter(id=alias.id).exists())
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['Joe Yan'])
        self.assertTrue(Participant.objects.filter(id=keep.id).exists())

    def test_map_requires_existing_target(self):
        """保留名不存在时直接报错，避免拼错名字静默新建联系人"""
        Participant.objects.create(user=self.user, name='Joe')
        with self.assertRaises(CommandError) as cm:
            call_command('merge_participants', '--map', 'Joe:Joe Yawn', stdout=StringIO())
        self.assertIn('没有名为「Joe Yawn」', str(cm.exception))

    def test_map_skips_unknown_alias_and_dedupes_plan(self):
        """别名不存在时只提示不报错；与自动检测重叠时同一行只合并一次"""
        text = self._run('--map', 'Nobody:YYX')
        self.assertIn('没有匹配到别名', text)
        self.assertEqual(self._run('--map', 'yyx:YYX').count('合并「yyx」'), 1)


class DailyViewStatusTest(TestCase):
    """Daily 页分区口径：已完成的活动不占用「今日进行中/今日结束」，由「近期完成」承载"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.today = timezone.localdate()

    def test_done_activities_are_out_of_today_sections(self):
        Activity.objects.create(user=self.user, name='今日已打卡',
                                start_date=self.today, status='done')
        Activity.objects.create(user=self.user, name='跨度今日完成',
                                start_date=self.today - timedelta(days=1),
                                end_date=self.today, status='done')
        Activity.objects.create(user=self.user, name='今日取消',
                                start_date=self.today, status='cancelled')
        Activity.objects.create(user=self.user, name='今日待办',
                                start_date=self.today, status='planned')

        ctx = self.client.get(reverse('activities:daily')).context
        # 单日 planned 活动归 ongoing（既有口径），done/cancelled 不再出现在今日各区
        self.assertEqual([a.name for a in ctx['ongoing']], ['今日待办'])
        self.assertEqual([a.name for a in ctx['starting_today']], [])
        self.assertEqual([a.name for a in ctx['ending_today']], [])
        self.assertEqual(ctx['ongoing_count'], 1)
        self.assertEqual({a.name for a in ctx['recently_done']},
                         {'今日已打卡', '跨度今日完成'})

    def test_span_activity_still_in_ongoing(self):
        """未完成的跳天活动仍在「今日进行中」，不受本次收紧影响"""
        Activity.objects.create(user=self.user, name='新西兰之旅',
                                start_date=self.today - timedelta(days=1),
                                end_date=self.today + timedelta(days=1),
                                status='in_progress')
        ctx = self.client.get(reverse('activities:daily')).context
        self.assertEqual([a.name for a in ctx['ongoing']], ['新西兰之旅'])


class QuickParseWiringTest(TestCase):
    """快速记一笔前端通道：三处入口都复用 static/js/quick-parse.js

    init()/parse() 传的是元素 id 字符串，模板改了 id 就会静默失效
    （getElementById 返回 null → 交互没反应且无报错），这里把
    「配置里引用的 id 必须真实存在于同一页面」钉成测试。
    """

    # init() 里取值是元素 id 的选项（URL/文案类选项跳过）
    ID_KEYS = ('input', 'parseBtn', 'confirmBtn', 'closeBtn', 'editBtn',
               'preview', 'previewBody', 'errEl', 'sourceEl')

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='新西兰之旅')

    def _assert_wired(self, html, call_marker, check_ids=True):
        self.assertIn('js/quick-parse.js', html, '共用模块未加载')
        # 普通 <script src> 必须在 head，早于页面里的调用
        self.assertLess(html.index('js/quick-parse.js'), html.index(call_marker),
                        '共用模块必须在调用之前加载')
        if not check_ids:
            return
        start = html.index(call_marker)
        block = html[start:html.index('});', start)]
        ids = {}
        for key in self.ID_KEYS:
            m = re.search(r'\b%s:\s*[\'"]([^\'"]+)[\'"]' % key, block)
            if m:
                ids[key] = m.group(1)
        self.assertTrue(ids, f'未找到任何元素 id 配置：{block[:200]}')
        for key, value in ids.items():
            self.assertIn(f'id="{value}"', html,
                          f'配置 {key}: {value!r} 在页面上没有对应元素')

    def test_activity_list_page(self):
        html = self.client.get(reverse('activities:activity_list')).content.decode()
        self._assert_wired(html, 'PaQuickParse.init(')

    def test_detail_page_subtask_quick_entry(self):
        html = self.client.get(
            reverse('activities:activity_detail', args=[self.activity.id])).content.decode()
        self._assert_wired(html, 'PaQuickParse.init(')

    def test_form_page_reuses_parse_channel(self):
        """创建页只做「解析 → 回填表单」，共用 fetch 通道但不自己拼 CSRF"""
        html = self.client.get(reverse('activities:activity_create')).content.decode()
        self._assert_wired(html, 'PaQuickParse.parse(', check_ids=False)
        self.assertNotIn('function getCookie', html, '页面仍在自己解析 cookie 取 CSRF')


@override_settings(MEDIA_ROOT=os.path.join(tempfile.gettempdir(), 'pa-test-media'))
class AttachmentUploadTest(TestCase):
    """附件上传：详情页是整页 POST（无 hx-*），必须回跳而不是把 JSON 渲染成页面"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='新西兰之旅')
        self.url = reverse('activities:attachment_upload', args=[self.activity.id])

    def _file(self):
        return SimpleUploadedFile('行程单.txt', b'hello', content_type='text/plain')

    def test_plain_form_upload_redirects_back(self):
        resp = self.client.post(self.url, {'file': self._file()})
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse('activities:activity_detail', args=[self.activity.id]))
        attachment = Attachment.objects.get(activity=self.activity)
        self.assertEqual(attachment.filename, '行程单.txt')
        self.assertTrue(any('已上传' in m.message for m in resp.wsgi_request._messages))

    def test_fetch_upload_returns_json(self):
        resp = self.client.post(self.url, {'file': self._file()},
                               HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['filename'], '行程单.txt')
        self.assertFalse(data['is_image'])

    def test_missing_file_keeps_page_and_shows_error(self):
        resp = self.client.post(self.url, {})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertTrue(any('请选择文件' in m.message for m in resp.wsgi_request._messages))
