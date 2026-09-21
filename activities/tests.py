import json
import os
import re
import tempfile
from decimal import Decimal
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.test import (TestCase, TransactionTestCase, Client, SimpleTestCase,
                         override_settings)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User
from core.models import Tag
from core.tags import apply_tags, tag_names
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.contrib import admin
from django.utils import timezone
from django.db.models import Sum

from activities.models import Activity, ActivityLog, Attachment, Expense, Participant, CalendarFeed
from activities.views.daily_views import gather_daily
from notes.models import Note
from activities.parsing import parse_quick_input
from core.agent_registry import CandidateToolError, ToolError
from core.layout_asserts import assert_desktop_two_columns
from activities.services import (InputError, add_expense,
                                 create_activity_from_parsed, record_parsed_cost,
                                 start_due_activities)
from activities.utils import (get_daily_bucket, DAILY_BUCKET_NAME,
                              DAILY_BUCKET_MARKER, daily_bucket_q, is_daily_bucket,
                              exclude_daily_bucket, resolve_participants)


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
        result = self.tool['fn'](self.user, {'amount': 25, 'tags': ['餐饮'], 'note': '午饭'})
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
        result = self.tool['fn'](self.user, {'amount': 30, 'tags': ['交通'], 'note': '上海 打车 35'})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity.name, '出差上海')
        self.assertIn('出差上海', result['reply'])

    def test_with_target_original_path(self):
        """有 target 时走原匹配路径，行为不变"""
        activity = Activity.objects.create(user=self.user, name='周末露营')
        result = self.tool['fn'](self.user, {'target': '露营', 'amount': 120, 'tags': ['购物']})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity_id, activity.id)
        # 标签随费用落库，回复里带上标签后缀
        self.assertEqual(tag_names(expense), ['购物'])
        self.assertEqual(result['reply'], f'已为「周末露营」添加费用 ¥120.00（购物）')

    def test_no_target_date_overlap_unique(self):
        """无 target 时当日/昨日日期重叠的唯一进行中活动优先命中"""
        Activity.objects.create(
            user=self.user, name='桐庐旅行', status='in_progress',
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=1),
        )
        result = self.tool['fn'](self.user, {'amount': 66, 'tags': ['餐饮']})
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
        # 列表数据不含归属桶。不能用整页 HTML 断言：base.html 快记面板的
        # 「不挂活动，直接记入「日常开支」」兑底按钮文案合法地含桶名（2026-09-19 两步流）
        page_names = {a.name for a in response.context['activities']}
        self.assertNotIn(DAILY_BUCKET_NAME, page_names)
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
        apply_tags(a3, ['团建活动'])
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

    def test_status_multi_value(self):
        """status 支持逗号分隔多值（Web Push 深链用）；非法值被剔除"""
        Activity.objects.create(user=self.user, name='进行中', status='in_progress')
        Activity.objects.create(user=self.user, name='计划中', status='planned')
        Activity.objects.create(user=self.user, name='已完成', status='done')
        self.assertEqual(self._filter_names(status='in_progress,planned'),
                         {'进行中', '计划中'})
        # 单值行为不变
        self.assertEqual(self._filter_names(status='done'), {'已完成'})
        # 全非法值 = 不过滤
        self.assertEqual(len(self._filter_names(status='bogus,foo')), 3)

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


class ToolTargetNumericIdTest(TestCase):
    """target 为纯数字时按活动 ID 直达（2026-09-19，卡片/列表展示 #ID 后配套）

    用户看到 ID 后会说「为活动 3 添加费用」：按名称模糊匹配「3」必然误伤，
    必须在 _resolve_single 里拦住纯数字 target 改走 ID 查询。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        self.tool = get_tool('activities.add_expense')
        self.user = User.objects.create_user('testuser', password='test')

    def test_numeric_target_resolves_by_id_not_name(self):
        """target=纯数字 ID 命中对应活动（名称里没有数字，模糊匹配永远命不中）"""
        activity = Activity.objects.create(user=self.user, name='桐庐旅行')
        self.tool['fn'](self.user, {'target': str(activity.id), 'amount': 66})
        self.assertEqual(Expense.objects.get().activity_id, activity.id)

    def test_numeric_target_missing_id_raises_tool_error(self):
        with self.assertRaises(ToolError) as ctx:
            self.tool['fn'](self.user, {'target': '999999', 'amount': 1})
        self.assertIn('没有 ID 为 999999 的活动', str(ctx.exception))

    def test_numeric_target_of_other_user_raises_tool_error(self):
        """别人的活动 ID 不可见（可见性校验在 ID 分支同样生效）"""
        other = User.objects.create_user('someone', password='test')
        Activity.objects.create(user=other, name='别人的活动')
        with self.assertRaises(ToolError):
            self.tool['fn'](self.user, {'target': str(Activity.objects.first().id), 'amount': 1})
        self.assertEqual(Expense.objects.count(), 0)

    def test_name_target_with_digits_still_matches_by_name(self):
        """名称含数字的正常路径不受影响（target 非纯数字才走名称匹配）"""
        Activity.objects.create(user=self.user, name='项目2026')
        self.tool['fn'](self.user, {'target': '项目2026', 'amount': 10})
        self.assertEqual(Expense.objects.get().activity.name, '项目2026')


class ActivityIdDisplayTest(SimpleTestCase):
    """活动 ID 展示锁：对话卡片与列表页活动名旁都标 #N，供用户直接口述 ID 操作"""

    EXPECTED = {
        'templates/chat/cards/activity_card.html': ['#{{ card.id }}', '#{{ c.id }}'],
        'templates/chat/cards/activity_list_card.html': ['#{{ item.id }}'],
        'templates/chat/cards/candidates_card.html': ['#{{ item.id }}'],
        'templates/chat/cards/daily_brief_card.html': ['#{{ item.id }}'],
        'templates/chat/cards/today_brief_card.html': ['#{{ item.id }}'],
        'templates/activities/activity_list.html': ['#{{ activity.id }}'],
    }

    def test_all_surfaces_render_activity_id(self):
        root = Path(__file__).resolve().parent.parent
        for rel, markers in self.EXPECTED.items():
            html = (root / rel).read_text(encoding='utf-8')
            for m in markers:
                self.assertIn(m, html, f'{rel} 缺少活动 ID 标注 {m}')


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


class DailyGatherStatusTest(TestCase):
    """daily 分区口径（gather_daily，daily 简报卡与旧 daily 页共用的数据层）：
    已完成的活动不占用「今日进行中/今日结束」，由「近期完成」承载。
    daily 页面已下线，这里直接测数据层——口径不会因为渲染层消失而失效。"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()

    def _gather(self):
        return gather_daily(self.user)

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

        data = self._gather()
        # 单日 planned 活动归 ongoing（既有口径），done/cancelled 不再出现在今日各区
        self.assertEqual([a.name for a in data['ongoing']], ['今日待办'])
        self.assertEqual([a.name for a in data['starting_today']], [])
        self.assertEqual([a.name for a in data['ending_today']], [])
        self.assertEqual(data['ongoing_count'], 1)
        self.assertEqual({a.name for a in data['recently_done']},
                         {'今日已打卡', '跨度今日完成'})

    def test_span_activity_still_in_ongoing(self):
        """未完成的跳天活动仍在「今日进行中」，不受本次收紧影响"""
        Activity.objects.create(user=self.user, name='新西兰之旅',
                                start_date=self.today - timedelta(days=1),
                                end_date=self.today + timedelta(days=1),
                                status='in_progress')
        data = self._gather()
        self.assertEqual([a.name for a in data['ongoing']], ['新西兰之旅'])


class DailyPageRetiredTest(TestCase):
    """/daily/ 页面下线（2026-09-19）：路由保留但重定向回首页（= 对话列表），
    旧书签 / 推送 / 模板 url 标签不破；渲染层已删除，任何残留在 template 里都会 TemplateDoesNotExist"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def test_daily_url_redirects_home(self):
        for name in ('daily', 'activities:daily'):
            with self.subTest(name=name):
                r = self.client.get(reverse(name))
                self.assertEqual(r.status_code, 302)
                self.assertEqual(r['Location'], reverse('home'))

    def test_template_file_is_gone(self):
        from django.conf import settings as dj_settings
        self.assertFalse(
            (dj_settings.BASE_DIR / 'templates' / 'activities' / 'daily.html').exists())


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
        """创建页只做「解析 → 回填表单」：解析逻辑在共用脚本 activity-form.js，不自己拼 CSRF"""
        html = self.client.get(reverse('activities:activity_create')).content.decode()
        self.assertIn('js/quick-parse.js', html, '共用模块未加载')
        self.assertIn('js/activity-form.js', html, '表单组件脚本未加载')
        # 普通 <script src> 必须按依赖顺序加载（activity-form.js 的解析调用依赖 PaQuickParse）
        self.assertLess(html.index('js/quick-parse.js'), html.index('js/activity-form.js'),
                        '共用模块必须在表单组件脚本之前加载')
        js = (Path(__file__).resolve().parent.parent
              / 'static' / 'js' / 'activity-form.js').read_text(encoding='utf-8')
        self.assertIn('PaQuickParse.parse(', js, '表单组件未复用共用解析通道')
        self.assertNotIn('function getCookie', js, '表单组件仍在自己解析 cookie 取 CSRF')
        self.assertNotIn('function getCookie', html, '页面仍在自己解析 cookie 取 CSRF')


class FilterPanelUsabilityTest(TestCase):
    """筛选面板可用性：激活摘要 chips（折叠时可读、可单项移除）+ 标签列表收敛

    之前折叠条只有「N 项生效」，展开后 24+ 个标签 chip 铺三四行。
    折叠条必须能一眼看出生效的是哪几项、每项能单独 × 掉；
    标签区默认只露前 10 个（激活的标签无论排第几始终可见）。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def _html(self, **params):
        resp = self.client.get(reverse('activities:activity_list'), params or None)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def _remove_hrefs(self, html):
        return re.findall(r'href="(/activities/\?[^"]*)"[^>]*title="移除该筛选"', html)

    @staticmethod
    def _qs_dicts(hrefs):
        # href 里的 & 在 HTML 里是 &amp;，解析前要反转义
        from urllib.parse import parse_qsl, urlparse
        return [dict(parse_qsl(urlparse(u.replace('&amp;', '&')).query))
                for u in hrefs]

    def test_no_active_filters_no_chips(self):
        html = self._html()
        self.assertNotIn('title="移除该筛选"', html, '无筛选时不应出现摘要 chips')

    def test_active_chips_render_with_single_item_removal(self):
        html = self._html(status='planned', tag='旅游', keyword='旅行')
        for chip in ('状态：计划', '# 旅游', '关键词「旅行」'):
            self.assertIn(chip, html, f'折叠条缺少激活摘要 {chip!r}')
        # × 链接：去掉对应参数、保留其余筛选
        qs_list = self._qs_dicts(self._remove_hrefs(html))
        self.assertIn({'tag': '旅游', 'keyword': '旅行'}, qs_list,
                      '移除状态后应保留 tag/keyword')
        self.assertIn({'status': 'planned', 'keyword': '旅行'}, qs_list,
                      '移除标签后应保留 status/keyword')
        self.assertIn({'status': 'planned', 'tag': '旅游'}, qs_list,
                      '移除关键词后应保留 status/tag')

    def test_date_filter_removed_as_one_item(self):
        html = self._html(date_from='2026-09-01', date_to='2026-09-21', status='planned')
        self.assertIn('日期 2026-09-01 ~ 2026-09-21', html)
        qs_list = self._qs_dicts(self._remove_hrefs(html))
        self.assertIn({'status': 'planned'}, qs_list,
                      '日期摘要应一次移除 date_from + date_to 两个参数')

    def _seed_overflow_tags(self):
        """造 20 个用过的标签（单活动受 MAX_TAGS_PER_OBJECT 限制，拆两个活动挂）"""
        for i in (1, 2):
            activity = Activity.objects.create(user=self.user, name=f'标签容器{i}')
            apply_tags(activity, [f'标签{(i - 1) * 10 + j:02d}' for j in range(1, 11)])

    def test_tag_list_capped_at_ten_with_more_toggle(self):
        self._seed_overflow_tags()
        html = self._html()
        self.assertIn('更多 10 个标签', html)
        # 计数只看「标签筛选」块内：活动卡片自身也会渲染 ?tag= 链接，不能全页数
        tag_block = html.split('标签筛选', 1)[1]
        visible, rest = tag_block.split('id="tag-more"', 1)
        rest = rest.split('id="tag-more-toggle"', 1)[0]
        self.assertEqual(visible.count('href="?tag='), 10, '可见区只应露 10 个标签')
        self.assertEqual(rest.count('href="?tag='), 10, '其余应收进隐藏区')

    def test_overflow_active_tag_always_visible(self):
        """激活的标签即使排在第 11+ 位也不能被收进「更多」，否则用户看不出自己选了什么"""
        self._seed_overflow_tags()
        # 从无筛选页取隐藏区第一个标签名
        html = self._html()
        rest = html.split('id="tag-more"', 1)[1]
        hidden_name = re.search(r'href="\?tag=([^&"]+)', rest).group(1)
        from urllib.parse import unquote
        tag_name = unquote(hidden_name)
        html2 = self._html(tag=tag_name)
        tag_block2 = html2.split('标签筛选', 1)[1]
        visible2 = tag_block2.split('id="tag-more"', 1)[0]
        self.assertIn(f'# {tag_name}', visible2, '激活的溢出标签必须在可见区')
        hidden2 = tag_block2.split('id="tag-more"', 1)[1].split('id="tag-more-toggle"', 1)[0]
        self.assertEqual(hidden2.count(f'tag={hidden_name}'), 0,
                         '激活的溢出标签不应重复出现在隐藏区')


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


class CostVsBudgetParsingTest(TestCase):
    """「预算 X」不再写入任何字段（预算上限已随 Activity.budget 字段删除）

    以前「预算 500」写 budget、「花了 300」写 cost；字段删除后只保留费用解析。
    """

    TODAY = date(2026, 8, 31)   # 周一

    def test_budget_keyword_writes_nothing(self):
        result = parse_quick_input('下周五团建预算500元', self.TODAY)
        self.assertNotIn('budget', result)
        self.assertNotIn('cost', result)

    def test_spent_keywords_write_cost(self):
        for text, amount in [('聚餐费用2千', 2000.0), ('花了300', 300.0), ('打车500元', 500.0)]:
            result = parse_quick_input(text, self.TODAY)
            self.assertEqual(result.get('cost'), amount, text)


class CostVsBudgetEndpointsTest(TestCase):
    """费用记支出，两条入口（快速创建 / 新建表单）口径一致"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def test_quick_create_expense_writes_expense(self):
        resp = self.client.post(
            reverse('activities:activity_quick_create'),
            data=json.dumps({'name': '团建', 'cost': 120}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        activity = Activity.objects.get(name='团建')
        self.assertEqual(activity.expenses.count(), 1)
        expense = activity.expenses.first()
        self.assertEqual(expense.amount, Decimal('120.00'))
        # 分类已下线：不传 tags 时费用不带任何归类
        self.assertEqual(tag_names(expense), [])

    def _post_create(self, payload):
        """提交新建表单（status 是必填项，模板靠 select 默认值带上）

        重定向响应没有 context，校验失败信息要先判空，否则断言语句本身会抛 TypeError。
        """
        resp = self.client.post(reverse('activities:activity_create'), {
            'participants_input': '', 'new_children': '', 'tags': '',
            'status': 'planned', **payload,
        })
        errors = resp.context['form'].errors if resp.context is not None else None
        self.assertEqual(resp.status_code, 302, errors)
        return resp

    def test_create_form_persisted_cost_becomes_expense(self):
        """表单里的「本次费用」记一笔支出"""
        self._post_create({'name': '周末行程', 'start_date': '2026-09-05',
                           'parsed_cost': '330.50'})
        activity = Activity.objects.get(name='周末行程')
        self.assertEqual(Expense.objects.filter(activity=activity).count(), 1)
        self.assertEqual(Expense.objects.get(activity=activity).amount, Decimal('330.50'))

    def test_zero_or_negative_cost_is_not_recorded(self):
        for i, raw in enumerate(('0', '-5')):
            self._post_create({'name': f'不记账{i}', 'parsed_cost': raw})
            Activity.objects.get(name=f'不记账{i}')
        self.assertEqual(Expense.objects.count(), 0)

    def test_edit_page_does_not_offer_cost_field(self):
        """编辑页不能再记“第一笔支出”，避免二次计费（详情页已有费用区）"""
        activity = Activity.objects.create(user=self.user, name='已存在')
        html = self.client.get(reverse('activities:activity_edit', args=[activity.id])).content.decode()
        # 页面脚本里引用了 id_parsed_cost（创建/编辑共用同一模板），断言必须是输入元素本身
        self.assertNotIn('name="parsed_cost"', html)
        self.assertNotIn('本次费用', html)
        create_html = self.client.get(reverse('activities:activity_create')).content.decode()
        self.assertIn('name="parsed_cost"', create_html, '创建页丢了「本次费用」入口')


class AiParseWeekAnchorTest(TestCase):
    """给 AI 的提示必须自带日历对照表，不能让它自己推周基准

    实测云端模型把周日（2026-08-30）的「下周五」推到了下下周五，
    而规则解析给的是 09-04；表格注入后两边口径对齐（已拿真实模型回环验证）。
    """

    def test_anchor_table_matches_rule_parser_for_next_weekday(self):
        from activities.views import _week_anchor_text
        for today in (date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 4), date(2026, 9, 6)):
            anchor = _week_anchor_text(today)
            expected = parse_quick_input('下周五', today)['start_date']
            self.assertIn(f'周五={expected}', anchor,
                          f'{today}（{today.strftime("%a")}）的锚点表与规则解析不一致')

    def test_anchor_declares_monday_as_week_start(self):
        from activities.views import _week_anchor_text
        anchor = _week_anchor_text(date(2026, 8, 30))
        self.assertIn('周一开始', anchor)
        self.assertIn('本周：周一=2026-08-24', anchor)
        self.assertIn('下周：周一=2026-08-31', anchor)

    def test_past_dates_flagged_for_bare_weekday_postpone(self):
        """裸「周X」已过时，表格要把本周那个日期标成已过去，否则模型会把活动排到过去

        只写文字约定时实测会返回「今天」（周日说「周六」→ 08-30），单元格标注才能驱动顺延。
        """
        from activities.views import _week_anchor_text
        today = date(2026, 8, 30)      # 周日，本周周六（08-29）已过
        anchor = _week_anchor_text(today)
        self.assertIn('周六=2026-08-29（已过去）', anchor)
        # 规则解析顺延后的目标日期必须能在下周行里找到
        expected = parse_quick_input('周六', today)['start_date']
        self.assertEqual(expected, '2026-09-05')
        self.assertIn(f'周六={expected}', anchor)
        self.assertIn('不得早于今天', anchor)
        # 今天本身不算已过去，不然「今天开会」会被推走
        self.assertNotIn('周日=2026-08-30（已过去）', anchor)


class RelativeDateParsingTest(TestCase):
    """规则解析补上往回看的相对日期（AI 不可用时的降级路径）

    此前只认今天/明天/后天/N天后：「昨天打车28元」会整条丢掉日期，
    费用落不到花钱那天 → 今日/本周消费统计少一笔。「上周X」旧口径会被
    当成裸「周X」推到下周去，方向刚好相反。
    """

    today = date(2026, 8, 31)      # 周一

    def parsed(self, text, today=None):
        return parse_quick_input(text, today or self.today)

    def test_past_relative_words(self):
        for word, iso in (('昨天', '2026-08-30'), ('前天', '2026-08-29'),
                          ('大前天', '2026-08-28')):
            result = self.parsed(f'{word}开会')
            self.assertEqual(result.get('start_date'), iso, word)
            self.assertEqual(result.get('end_date'), iso, word)

    def test_n_days_before_and_after(self):
        self.assertEqual(self.parsed('3天后出发')['start_date'], '2026-09-03')
        self.assertEqual(self.parsed('5天前买的机票')['start_date'], '2026-08-26')

    def test_past_day_expense_keeps_date_and_amount(self):
        result = self.parsed('昨天打车28元')
        self.assertEqual(result['cost'], 28.0)
        self.assertEqual(result['start_date'], '2026-08-30')

    def test_last_week_dates_go_back(self):
        self.assertEqual(self.parsed('上周六爬山')['start_date'], '2026-08-29')
        self.assertEqual(self.parsed('上周日野餐')['start_date'], '2026-08-30')
        self.assertEqual(self.parsed('上上周五开会')['start_date'], '2026-08-21')

    def test_future_paths_unchanged(self):
        self.assertEqual(self.parsed('下周三开会')['start_date'], '2026-09-09')
        self.assertEqual(self.parsed('本周五团建')['start_date'], '2026-09-04')
        # 裸「周X」未过 → 取本周，已过 → 顺延下周（旧口径保留）
        self.assertEqual(self.parsed('周三例会')['start_date'], '2026-09-02')
        self.assertEqual(self.parsed('周六', date(2026, 8, 30))['start_date'], '2026-09-05')

    def test_anchor_table_covers_last_week_too(self):
        """AI 锚点表与规则解析同口径，否则降级前后给出的日期不同"""
        from activities.views import _week_anchor_text
        anchor = _week_anchor_text(self.today)
        for phrase in ('上周六', '上周日', '下周三'):
            expected = self.parsed(phrase)['start_date']
            self.assertIn(f'={expected}', anchor, phrase)
        self.assertIn('不得早于今天', anchor)


class TimeParsingTest(TestCase):
    """规则解析识别具体时间表述（2026-09-17）：「明天下午3点」→ 15:00

    原则与日期一致：只提取能明确识别的时间，歧义不猜；
    无日期的孤时间不输出（无法落到日历）。
    """

    def parsed(self, text):
        return parse_quick_input(text, date(2026, 9, 17))

    def test_period_word_and_half(self):
        d = self.parsed('明天下午3点半看牙')
        self.assertEqual(d['start_date'], '2026-09-18')
        self.assertEqual(d['start_time'], '15:30')
        self.assertEqual(d['name'], '看牙')

    def test_clock_format_and_end_time(self):
        d = self.parsed('9月20日14:30到16:00开会')
        self.assertEqual(d['start_date'], '2026-09-20')
        self.assertEqual(d['start_time'], '14:30')
        self.assertEqual(d['end_time'], '16:00')
        self.assertEqual(d['name'], '开会')

    def test_various_periods(self):
        # 文本须带日期：孤时间按设计不输出（见 test_orphan_time_without_date_dropped）
        self.assertEqual(self.parsed('明天晚上8点跑步')['start_time'], '20:00')
        self.assertEqual(self.parsed('明天上午9点体检')['start_time'], '09:00')
        self.assertEqual(self.parsed('明天中午12点聚餐')['start_time'], '12:00')
        self.assertEqual(self.parsed('明天中午1点吃饭')['start_time'], '13:00')
        self.assertEqual(self.parsed('明天15点取快递')['start_time'], '15:00')

    def test_ambiguous_bare_hour_not_guessed(self):
        """裸「3点」无词头不猜上下午；不产生时间，名称保留原文"""
        d = self.parsed('明天3点出门')
        self.assertNotIn('start_time', d)
        self.assertIn('3点', d['name'])

    def test_orphan_time_without_date_dropped(self):
        """只有时间没有日期 → 不输出时间（孤时间落不到日历）"""
        d = self.parsed('下午3点开会')
        self.assertNotIn('start_time', d)
        self.assertNotIn('start_date', d)

    def test_time_spans_removed_from_name(self):
        d = self.parsed('下周三早上7点半晨跑')
        self.assertEqual(d['name'], '晨跑')
        self.assertEqual(d['start_time'], '07:30')

    def test_normalize_time_and_orphan_drop(self):
        from activities.utils import normalize_input
        out = normalize_input({'name': 'x', 'start_date': '2026-09-18',
                               'start_time': '15:00', 'end_time': '9:30'}, date(2026, 9, 17))
        self.assertEqual(out['start_time'], '15:00')
        self.assertEqual(out['end_time'], '09:30')
        out = normalize_input({'name': 'x', 'start_time': '15:00'}, date(2026, 9, 17))
        self.assertNotIn('start_time', out)   # 无日期孤时间丢弃
        out = normalize_input({'name': 'x', 'start_time': '25:00'}, date(2026, 9, 17))
        self.assertNotIn('start_time', out)   # 越界丢弃


class TimedActivityModelTest(TestCase):
    """Activity 具体时间字段：date_range 展示与表单校验"""

    def setUp(self):
        self.user = User.objects.create_user('tu', password='x')

    def test_date_only_unchanged(self):
        """只填日期不填时间：展示与原先完全一致"""
        a = Activity.objects.create(user=self.user, name='x',
                                    start_date=date(2026, 9, 20), end_date=date(2026, 9, 21))
        self.assertEqual(a.date_range, '2026-09-20 ~ 2026-09-21')
        single = Activity.objects.create(user=self.user, name='y', start_date=date(2026, 9, 20))
        self.assertEqual(single.date_range, '2026-09-20')

    def test_date_range_with_times(self):
        from datetime import time
        a = Activity.objects.create(user=self.user, name='x', start_date=date(2026, 9, 20),
                                    end_date=date(2026, 9, 20), start_time=time(14, 0),
                                    end_time=time(16, 0))
        self.assertEqual(a.date_range, '2026-09-20 14:00 ~ 16:00')
        multi = Activity.objects.create(user=self.user, name='y', start_date=date(2026, 9, 20),
                                        end_date=date(2026, 9, 22), start_time=time(9, 0))
        self.assertEqual(multi.date_range, '2026-09-20 09:00 ~ 2026-09-22')

    def test_form_requires_date_for_time(self):
        from activities.forms import ActivityForm
        # 时间字段是 HourMinuteSelect 双下拉，POST 字段名为 start_time_0（时）/ _1（分）
        form = ActivityForm({'name': 'x', 'status': 'planned',
                             'start_time_0': '15', 'start_time_1': '00'},
                            user=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn('start_time', form.errors)
        # 带日期则通过，且时间字段被保存
        form = ActivityForm({'name': 'x', 'status': 'planned',
                             'start_date': '2026-09-20',
                             'start_time_0': '15', 'start_time_1': '00',
                             'end_time_0': '16', 'end_time_1': '00'}, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)   # user 由视图层注入，表单不含该字段
        obj.user = self.user
        obj.save()
        self.assertEqual(str(obj.start_time)[:5], '15:00')

    def test_create_from_parsed_passes_times(self):
        from activities.utils import normalize_input
        data = normalize_input({'name': '牙医', 'start_date': '2026-09-18',
                                'start_time': '15:30'}, date(2026, 9, 17))
        result = create_activity_from_parsed(self.user, data, source='测试')
        a = result['activity']
        self.assertEqual(str(a.start_time), '15:30')
        self.assertIsNone(a.end_time)


class DailyBucketSingleDefinitionTest(TestCase):
    """「日常开支」归属桶单一判定（H5）

    取桶 / 内存判定 / 查询集排除必须共用 marker 条件。以前取桶只按 name，
    用户自建一个同名活动就会被静默收养成系统桶。
    """

    def setUp(self):
        self.user = User.objects.create_user('bucketuser', password='p')

    def test_user_created_same_name_activity_is_not_adopted(self):
        mine = Activity.objects.create(user=self.user, name=DAILY_BUCKET_NAME,
                                       description='我自己建的记账活动')
        bucket = get_daily_bucket(self.user)
        self.assertNotEqual(bucket.id, mine.id)
        self.assertIn(DAILY_BUCKET_MARKER, bucket.description)
        # 用户自建那条仍是普通活动，不会被当成桶排除
        listed = list(exclude_daily_bucket(Activity.objects.filter(user=self.user)))
        self.assertEqual(listed, [mine])

    def test_bucket_reused_across_calls(self):
        first = get_daily_bucket(self.user)
        self.assertEqual(get_daily_bucket(self.user).id, first.id)
        self.assertEqual(
            Activity.objects.filter(user=self.user, description__contains=DAILY_BUCKET_MARKER).count(), 1)

    def test_in_memory_check_agrees_with_queryset_condition(self):
        """内存版与 Q 版不能走形（三处口径曾经不一致的根因）"""
        bucket = get_daily_bucket(self.user)
        same_name_no_marker = Activity.objects.create(user=self.user, name=DAILY_BUCKET_NAME)
        other_name_with_marker = Activity.objects.create(
            user=self.user, name='其他活动', description=DAILY_BUCKET_MARKER)
        for act in (bucket, same_name_no_marker, other_name_with_marker):
            self.assertEqual(
                is_daily_bucket(act),
                Activity.objects.filter(pk=act.pk).filter(daily_bucket_q()).exists(),
                f'活动 {act.pk} 的内存判定与查询集口径不一致')

    def test_auto_expense_keyword_branch_skips_system_bucket(self):
        """关键词兜底不得把系统桶当命中目标（仍走「桶兜底」分支）"""
        from activities.agent_tools import _auto_expense_target
        bucket = get_daily_bucket(self.user)
        target, reason = _auto_expense_target(self.user, '日常开支 打车')
        self.assertEqual(target.id, bucket.id)
        self.assertEqual(reason, 'bucket')



class WritePathServiceTest(TestCase):
    """M1 写路径收敛：创建与记费用只留 services 一份实现

    锁住收敛后的四条口径：空值/0 的金额语义、日期的单一回落、
    标签统一走 core.Tag（scope='expense'）、子活动归属继承父活动。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.admin = User.objects.create_superuser('root', password='p')
        self.activity = Activity.objects.create(user=self.user, name='桐庐周末游')
        self.client = Client()

    def test_empty_amount_records_nothing(self):
        for raw in ('', None, '   '):
            self.assertIsNone(add_expense(self.activity, self.user, raw), repr(raw))
        self.assertEqual(Expense.objects.count(), 0)

    def test_zero_amount_semantics_per_entry(self):
        # 「记一笔」类入口：0 元没有意义 → 直接报错
        with self.assertRaises(InputError):
            add_expense(self.activity, self.user, 0, positive=True)
        # 解析类入口：0 视为「本次没花钱」→ 静默跳过，不建 0 元记录
        self.assertIsNone(record_parsed_cost(self.activity, self.user, '0'))
        self.assertIsNone(record_parsed_cost(self.activity, self.user, '-5'))
        self.assertEqual(Expense.objects.count(), 0)

    def test_paid_at_single_fallback_rule(self):
        """日期只有一套规则：未传/空/非法 → 今天；clear_date 仅用于派生记录"""
        today = timezone.localdate()
        self.assertEqual(add_expense(self.activity, self.user, 10).paid_at, today)
        self.assertEqual(add_expense(self.activity, self.user, 10, paid_at='').paid_at, today)
        self.assertEqual(add_expense(self.activity, self.user, 10, paid_at='昨天').paid_at,
                         today)
        self.assertEqual(add_expense(self.activity, self.user, 10,
                                    paid_at='2026-08-01').paid_at, date(2026, 8, 1))
        self.assertIsNone(add_expense(self.activity, self.user, 10, clear_date=True).paid_at)

    def test_tags_persist_through_add_expense(self):
        """标签是费用唯一归类：add_expense 的 tags 参数（字符串或数组）直达 Expense.tags"""
        expense = add_expense(self.activity, self.user, 66, tags='餐饮, 交通')
        self.assertEqual(set(tag_names(expense)), {'餐饮', '交通'})
        # 不传 tags 时保持为空
        self.assertEqual(tag_names(add_expense(self.activity, self.user, 10)), [])

    def test_child_created_by_superuser_keeps_parent_owner(self):
        """AGENTS.md：子活动归属继承父活动，超管建的下级仍归原主人"""
        parent = Activity.objects.create(user=self.user, name='新西兰之旅')
        result = create_activity_from_parsed(self.admin, {'name': '订机票'},
                                            parent=parent, source='AI 对话')
        child = result['activity']
        self.assertEqual(child.user_id, self.user.id)
        logged = set(ActivityLog.objects.values_list('activity_id', 'action', 'user_id'))
        self.assertIn((parent.id, 'sub_created', self.admin.id), logged)
        self.assertIn((child.id, 'created', self.admin.id), logged)

    def test_quick_sub_cost_lands_on_child_not_parent(self):
        """一句话建子任务：花费记在子任务名下（与内联手动表单同口径）"""
        parent = Activity.objects.create(user=self.user, name='新西兰之旅')
        self.client.login(username='testuser', password='test')
        resp = self.client.post(
            reverse('activities:activity_quick_sub', args=[parent.id]),
            data=json.dumps({'name': '办签证', 'cost': 350}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        child = Activity.objects.get(name='办签证')
        self.assertEqual(Expense.objects.filter(activity=child).count(), 1)
        self.assertEqual(Expense.objects.filter(activity=parent).count(), 0)
        self.assertEqual(Expense.objects.get(activity=child).amount, Decimal('350.00'))

    def test_add_subactivity_endpoint_keeps_both_logs(self):
        """快捷建子任务改走 services 后，仍要写父+子两条日志且只建一个对象"""
        parent = Activity.objects.create(user=self.user, name='意大利之旅')
        self.client.login(username='testuser', password='test')
        resp = self.client.post(reverse('activities:add_subactivity', args=[parent.id]),
                               {'name': '租车'})
        self.assertEqual(resp.status_code, 302)
        child = Activity.objects.get(name='租车')
        self.assertEqual(child.parent_id, parent.id)
        self.assertEqual(child.user_id, self.user.id)
        self.assertEqual(child.end_date, timezone.localdate())
        self.assertEqual(sorted(ActivityLog.objects.values_list('action', flat=True)),
                         ['created', 'sub_created'])

    def test_quick_expense_endpoint_requires_amount(self):
        """全局快记：空金额不再静默建 0 元记录，正常记入默认落今天"""
        self.client.login(username='testuser', password='test')
        resp = self.client.post(reverse('activities:expense_quick_create'), {'amount': ''})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.json())
        self.assertEqual(Expense.objects.count(), 0)

        resp = self.client.post(reverse('activities:expense_quick_create'),
                               {'amount': '28.5', 'tags': '餐饮', 'note': '午饭'})
        self.assertEqual(resp.status_code, 200, resp.content)
        expense = Expense.objects.get()
        self.assertEqual(expense.amount, Decimal('28.50'))
        self.assertEqual(tag_names(expense), ['餐饮'])
        self.assertEqual(expense.activity.name, DAILY_BUCKET_NAME)
        self.assertEqual(expense.paid_at, timezone.localdate())


class ExpenseQuickCandidatesTest(TestCase):
    """快记费用两步流：提交后先弹归属选择卡（2026-09-19，用户要求）

    旧版直接落「日常开支」桶被用户认为不合理——无活动语境的快记应先让
    用户从「计划 + 进行中」的活动里选归属；桶只作为显式兑底按钮保留。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def _candidates(self, q=''):
        resp = self.client.get(reverse('activities:expense_quick_candidates'), {'q': q})
        self.assertEqual(resp.status_code, 200)
        return resp.json()['items']

    def test_login_required(self):
        out = Client().get(reverse('activities:expense_quick_candidates'))
        self.assertEqual(out.status_code, 302)

    def test_only_planned_and_in_progress_excludes_bucket_and_archived(self):
        planned = Activity.objects.create(user=self.user, name='周末爬山', status='planned')
        ongoing = Activity.objects.create(user=self.user, name='装修房子', status='in_progress')
        Activity.objects.create(user=self.user, name='老旅行', status='done')
        Activity.objects.create(user=self.user, name='黄了的局', status='cancelled')
        archived = Activity.objects.create(user=self.user, name='去年的项目', status='in_progress')
        archived.archived_at = timezone.now()
        archived.save(update_fields=['archived_at'])
        get_daily_bucket(self.user)
        names = [i['name'] for i in self._candidates()]
        self.assertEqual(set(names), {'周末爬山', '装修房子'})
        # 进行中优先
        self.assertEqual(names[0], '装修房子')
        self.assertNotIn(DAILY_BUCKET_NAME, names)

    def test_q_filters_by_name(self):
        Activity.objects.create(user=self.user, name='周末爬山', status='planned')
        Activity.objects.create(user=self.user, name='装修房子', status='in_progress')
        names = [i['name'] for i in self._candidates(q='爬山')]
        self.assertEqual(names, ['周末爬山'])

    def test_picked_activity_receives_expense(self):
        """选择卡点选后带 activity_id 提交：费用记到选中的活动而不是桶"""
        activity = Activity.objects.create(user=self.user, name='周末爬山', status='planned')
        resp = self.client.post(reverse('activities:expense_quick_create'),
                                {'amount': '66', 'activity_id': str(activity.id)})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['activity_name'], '周末爬山')
        self.assertEqual(Expense.objects.get().activity_id, activity.id)
        self.assertFalse(Activity.objects.filter(name=DAILY_BUCKET_NAME).exists())


class ExpenseQuickPickWiringTest(SimpleTestCase):
    """两步流前端接线锁：base.html 必须先取候选再展示选择卡，禁 innerHTML"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parent.parent
        cls.html = (root / 'templates' / 'base.html').read_text(encoding='utf-8')

    def test_submit_first_fetches_candidates_then_renders_pick_card(self):
        self.assertIn("url \"activities:expense_quick_candidates\"", self.html)
        self.assertIn("id=\"quick-expense-pick\"", self.html)
        # 提交 handler 里先拉候选再切视图
        submit_idx = self.html.index("expenseForm.addEventListener('submit'")
        fetch_idx = self.html.index('fetch(candidatesUrl', submit_idx)
        show_idx = self.html.index("expensePick.classList.remove('hidden')", fetch_idx)
        self.assertLess(fetch_idx, show_idx)

    def test_pick_list_built_without_innerhtml(self):
        pick_start = self.html.index('id="quick-expense-pick"')
        note_start = self.html.index('<!-- 备忘 Tab -->', pick_start)
        block = self.html[pick_start:note_start]
        self.assertNotIn('innerHTML', block)
        # 列表行只能 createElement + textContent 构建（活动名是用户数据）
        self.assertIn('pickListEl.textContent', self.html)


class StatusDisplaySingleSourceTest(TestCase):
    """M3 状态展示统一：颜色只在 custom.css 定义一处

    收敛前同一组状态有 4 份颜色映射（徽章、圆点/色条、日历 JS、Daily 图标），
    done 与 cancelled 的深浅在日历与徽章之间正好相反。这里锁住两件事：
    CSS 必须为每个 STATUS_CHOICES 取值补齐工具类，模板与 JSON 不得再存一份映射。
    """

    # 历史坏味道：在状态分支里直接写 Tailwind 灰阶类
    LEGACY_COLOR_BRANCH = re.compile(r"status\s*==\s*'[^']+'\s*%>[^%]*?-(zinc|gray|neutral)-\d00")

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def css(self):
        return (Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css').read_text(
            encoding='utf-8')

    def test_css_defines_every_status_utility(self):
        """新增状态取值时不得漏配色：四个工具类与变量都需存在"""
        css = self.css()
        for value, _label in Activity.STATUS_CHOICES:
            self.assertIn(f'--status-{value}:', css)
            for prefix in ('status-bg--', 'status-fg--', 'status-fg-on--', 'badge-status--'):
                self.assertIn(f'.{prefix}{value}', css, f'{prefix}{value} 缺定义')

    def test_templates_have_no_legacy_status_color_branch(self):
        templates_dir = Path(settings.BASE_DIR) / 'templates'
        offenders = [str(t.relative_to(templates_dir)) for t in templates_dir.rglob('*.html')
                     if self.LEGACY_COLOR_BRANCH.search(t.read_text(encoding='utf-8'))]
        self.assertEqual(offenders, [])

    def test_badge_and_bar_render_from_status_class(self):
        activity = Activity.objects.create(user=self.user, name='桐庐周末游',
                                           status='in_progress')
        html = self.client.get(
            reverse('activities:activity_detail', args=[activity.id])).content.decode()
        self.assertIn('badge-status--in_progress', html)
        self.assertIn('status-bg--in_progress', html)

    def test_calendar_payload_carries_status_not_color(self):
        """颜色不在接口层下发，否则前端与后端各一份必然漂移"""
        Activity.objects.create(user=self.user, name='桐庐周末游', status='done',
                               start_date=timezone.localdate())
        item = self.client.get(reverse('activities:calendar_data')).json()['activities'][0]
        self.assertEqual(item['status'], 'done')
        self.assertNotIn('color', item)

    def test_calendar_legend_covers_every_status(self):
        html = self.client.get(reverse('activities:activity_calendar')).content.decode()
        for value, label in Activity.STATUS_CHOICES:
            self.assertIn(f'status-bg--{value}', html)
            self.assertIn(label, html)


class StartDueActivitiesTest(TestCase):
    """L10 到期自动转进行中：cron 与页面访问共用一份判定，dry-run 不漂移

    收敛前 filter 写了两份（命令的 --dry-run 可能与真实执行不一致），
    且命令调的是 views 里的函数（写路径反向依赖视图层）。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.system = User.objects.create_user('system', password='p')

    def test_due_planned_starts_while_future_and_cancelled_stay(self):
        due = Activity.objects.create(user=self.user, name='今天开始', status='planned',
                                      start_date=timezone.localdate())
        future = Activity.objects.create(user=self.user, name='下周开始', status='planned',
                                         start_date=timezone.localdate() + timedelta(days=7))
        cancelled = Activity.objects.create(user=self.user, name='已取消', status='cancelled',
                                            start_date=timezone.localdate())
        changed = start_due_activities(self.user)
        self.assertEqual([a.id for a in changed], [due.id])
        for a in (due, future, cancelled):
            a.refresh_from_db()
        self.assertEqual(
            [due.status, future.status, cancelled.status],
            ['in_progress', 'planned', 'cancelled'])
        log = ActivityLog.objects.get(activity=due)
        self.assertEqual((log.user_id, log.action), (self.system.id, 'status_changed'))

    def test_dry_run_previews_the_same_set_without_writing(self):
        due = Activity.objects.create(user=self.user, name='今天开始', status='planned',
                                      start_date=timezone.localdate())
        preview = start_due_activities(dry_run=True)
        self.assertEqual([a.id for a in preview], [due.id])
        due.refresh_from_db()
        self.assertEqual(due.status, 'planned')
        self.assertEqual(ActivityLog.objects.count(), 0)
        # 真实执行必须是同一批（否则 --dry-run 的结果不可信）
        self.assertEqual([a.id for a in start_due_activities()], [a.id for a in preview])

    def test_missing_system_user_skips_log_but_still_starts(self):
        """容错铁律：日志这类非核心操作失败只告警，不阻断主流程"""
        due = Activity.objects.create(user=self.user, name='今天开始', status='planned',
                                      start_date=timezone.localdate())
        self.system.delete()
        changed = start_due_activities()
        due.refresh_from_db()
        self.assertEqual([a.id for a in changed], [due.id])
        self.assertEqual(due.status, 'in_progress')
        self.assertEqual(ActivityLog.objects.count(), 0)


class UpdateDescriptionAgentToolTest(TestCase):
    """AI 把对话结论写进活动描述：默认追加，显式 description_mode=replace 才整段覆盖

    这条口径的由来：模型看不到活动原有描述的全文，允许它直接覆盖等于给了一个
    「一句话冲掉用户长文本」的按钮，所以覆盖必须显式声明。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        self.user = User.objects.create_user('testuser', password='test')
        self.tool = get_tool('activities.update')

    def test_append_keeps_original_description(self):
        activity = Activity.objects.create(user=self.user, name='桐庐周末游',
                                           description='原计划：住山脚民宿')
        result = self.tool['apply'](self.user, {
            'target_id': activity.id, 'description': '结论：高铁比自驾省 3 小时'})
        activity.refresh_from_db()
        self.assertIn('原计划：住山脚民宿', activity.description)
        self.assertIn('结论：高铁比自驾省 3 小时', activity.description)
        self.assertTrue(result['changed'])

    def test_replace_mode_overwrites(self):
        activity = Activity.objects.create(user=self.user, name='桐庐周末游',
                                           description='旧描述')
        self.tool['apply'](self.user, {'target_id': activity.id, 'description': '全新描述',
                                       'description_mode': 'replace'})
        activity.refresh_from_db()
        self.assertEqual(activity.description, '全新描述')

    def test_empty_description_treated_as_replace(self):
        activity = Activity.objects.create(user=self.user, name='桐庐周末游')
        self.tool['apply'](self.user, {'target_id': activity.id, 'description': '第一条备注'})
        activity.refresh_from_db()
        self.assertEqual(activity.description, '第一条备注')

    def test_preview_shows_append_and_writes_nothing(self):
        activity = Activity.objects.create(user=self.user, name='桐庐周末游',
                                           description='原计划：住山脚民宿')
        preview = self.tool['fn'](self.user, {'target': '桐庐周末游',
                                              'description': '结论：高铁更快'})
        self.assertEqual(preview['card'], 'confirm')
        change = next(c for c in preview['card_data']['changes'] if c['field'] == 'description')
        # 确认卡必须说清「保留原文」，否则用户会以为要被覆盖
        self.assertIn('保留原文', change['new'])
        self.assertIn('原计划', change['old'])
        activity.refresh_from_db()
        self.assertEqual(activity.description, '原计划：住山脚民宿')   # 预览不写库

    def test_long_description_is_abbreviated_in_change_and_log(self):
        long_old = '长' * 120
        activity = Activity.objects.create(user=self.user, name='桐庐周末游',
                                           description=long_old)
        self.tool['apply'](self.user, {'target_id': activity.id, 'description': '补一句结论'})
        log = ActivityLog.objects.filter(activity=activity).order_by('-id').first()
        self.assertIn('描述', log.summary)
        self.assertNotIn(long_old, log.summary)   # 整段原文不贴进日志
        self.assertIn('…', log.summary)
        self.assertLess(len(log.summary), 200)

    def test_prompt_advertises_description_capability(self):
        """协议里要写清能改描述、且默认是追加——否则模型只会继续回避这个字段"""
        from core.agent_registry import build_protocol_prompt
        prompt = build_protocol_prompt()
        self.assertIn('description 描述', prompt)
        self.assertIn('默认追加到原描述末尾', prompt)
        self.assertIn('description_mode="replace"', prompt)

    def test_other_fields_keep_untouched_on_description_only_update(self):
        activity = Activity.objects.create(user=self.user, name='桐庐周末游',
                                           start_date=date(2026, 9, 5))
        self.tool['apply'](self.user, {'target_id': activity.id, 'description': '只改描述'})
        activity.refresh_from_db()
        self.assertEqual(activity.start_date, date(2026, 9, 5))


def _css_rules(css, selector):
    """列声明检查用的 CSS 规则解析。实现已抽到 core.layout_asserts（六个 app 的
    布局锁共用一份），这里只保留局部别名，免改下面两处调用点。"""
    from core.layout_asserts import css_rules
    return css_rules(css, selector)


class ActivityDetailDesktopLayoutTest(TestCase):
    """活动详情页桌面两列布局与概览增强回归锁

    为什么锁：两列完全靠 custom.css 的通用列容器 .page-cols + 模板里两个列容器实现，
    没有任何报错机制——顺手改回单列、把右列某块挪进左列、给移动端加了 sm: 结构断点，
    都只在真实屏幕上退化。移动端同理：列容器不加显隐，视觉顺序 = DOM 顺序，
    所以 DOM 里各块的先后就是移动端顺序契约。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'activities' / 'activity_detail.html'
    CSS = Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css'
    # 列分隔靠这两条注释锚点（模板里显式写出，改结构时会一起被看到）
    LEFT_END = '</div><!-- /左列 -->'
    COLS_END = '</div><!-- /.page-cols -->'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        self.parent = Activity.objects.create(
            user=self.user, name='新西兰之旅', description='南岛自驾')
        Activity.objects.create(user=self.user, name='订机票', parent=self.parent, status='done')
        Activity.objects.create(user=self.user, name='租车', parent=self.parent, status='planned')
        expense = Expense.objects.create(activity=self.parent, user=self.user,
                                         amount=Decimal('500'))
        apply_tags(expense, ['餐饮'])
        # 右列三个条件渲染块（参与者/关联等）不带数据就整块不渲染，顺序锁会空跑
        apply_tags(self.parent, ['自驾'])
        self.parent.participants.add(
            Participant.objects.create(user=self.user, name='小王'))
        note = Note.objects.create(user=self.user, content='新西兰南岛自驾路线草稿')
        apply_tags(note, ['自驾'])
        ActivityLog.objects.create(user=self.user, activity=self.parent,
                                   action='created', summary='手动创建')
        resp = self.client.get(f'/activities/{self.parent.id}/')
        self.html = resp.content.decode()

    def _columns(self):
        start = self.html.index('class="page-cols')
        return self.html[start:self.html.index(self.COLS_END)]

    def test_two_column_grid_and_exactly_two_columns(self):
        self.assertEqual(self.html.count('class="page-cols'), 1, '两列容器应唯一')
        cols = self._columns()
        self.assertEqual(cols.count('class="page-main '), 1,
                         'page-cols 内应恰好一个左列（主内容流）')
        self.assertEqual(cols.count('class="page-rail '), 1,
                         'page-cols 内应恰好一个右列（辅助信息）')
        self.assertIn(self.LEFT_END, cols, '左列未闭合，两列结构已破损')

    def test_no_template_syntax_leaked_into_rendered_html(self):
        """模板注释语法泄漏锁：Django 的 {# #} 不支持跨行，写多行会整段渲染成正文"""
        for token in ('{%', '{{', '{#'):
            self.assertNotIn(token, self.html, f'渲染结果里出现 {token}，模板语法泄漏')

    def test_primary_flow_left_auxiliary_right(self):
        cols = self._columns()
        left, right = cols.split(self.LEFT_END, 1)
        for anchor, desc in [('活动描述', '描述'), ('id="expense-form"', '记一笔表单'),
                             ('id="subtask-list"', '子任务时间轴')]:
            self.assertIn(anchor, left, f'{desc}应在左列主内容流')
            self.assertNotIn(anchor, right, f'{desc}不该出现在右列')
        for anchor, desc in [('id="attachment-form"', '附件'), ('参与者（', '参与者'),
                             ('相关知识与笔记', '关联内容')]:
            self.assertIn(anchor, right, f'{desc}应在右列辅助信息')

    def test_mobile_reading_order_matches_dom(self):
        """移动端列容器不带显隐类，视觉顺序 = DOM 顺序；这里锁住约定的顺序"""
        anchors = ['活动描述', 'id="expense-form"', 'id="subtask-list"',
                   'id="attachment-form"', '参与者（', '相关知识与笔记', '操作历史（']
        positions = [self.html.index(a) for a in anchors]
        self.assertEqual(positions, sorted(positions),
                         '移动端单列顺序变了（子任务应紧跟费用明细，右列整体在其后）')
        cols = self._columns()
        for c in re.findall(r'<div class="[^"]*">', cols)[:2]:
            self.assertNotIn('hidden', c, '列容器加了显隐类会让移动端少一整列')

    def test_quick_action_card_is_desktop_only_and_inside_right_column(self):
        cols = self._columns()
        right = cols.split(self.LEFT_END, 1)[1]
        card = right[:right.index('附件区域')]
        self.assertIn('hidden md:block', card,
                      '快捷操作卡必须只在桌面端出现，否则移动端白占高度')
        self.assertEqual(card.count('data-jump='), 3, '记一笔/加子任务/传附件三个入口缺一')

    def test_no_structural_sm_breakpoint_in_template(self):
        """分端只允许 md:（768px）；sm: 仅可作纯尺寸渐进（p/gap/text/space/w-[calc]）"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        hits = re.findall(r'sm:(hidden|block|flex|inline|grid|order|col-span|row-span|sticky|absolute|fixed)\S*', src)
        self.assertEqual(hits, [], f'模板出现 sm: 结构性断点：{hits}')

    def test_columns_css_uses_single_md_breakpoint(self):
        css = self.CSS.read_text(encoding='utf-8')
        cols = _css_rules(css, '.page-cols')
        self.assertTrue(cols, 'custom.css 里两列声明丢了')
        for block in cols:
            self.assertEqual(block['media'], '(min-width: 768px)',
                             '.page-cols 必须只落在 768px 这个唯一结构断点内')
        body = next(b['body'] for b in cols if b['selectors'] == '.page-cols')
        self.assertIn('320px', body, '右列固定宽度是本次设计的一部分')
        self.assertIn('minmax(0, 1fr)', body, '左列须用 minmax(0,1fr) 兜住长内容撑破列')
        self.assertIn('align-items: start', body,
                      '网格默认 stretch 会把右列拉高，sticky 就没空间钉住')
        rail = _css_rules(css, '.page-rail')
        self.assertTrue(rail, 'custom.css 里右列常驻声明丢了')
        self.assertIn('position: sticky', rail[0]['body'], '右列整列常驻是桌面端设计的一部分')
        self.assertIn('max-height', rail[0]['body'], '矮视口下右列需列内滚动，否则底部信息看不到')

    def test_subtask_progress_rendered(self):
        """概览增强：子任务完成度进度条"""
        self.assertIn('title="子任务完成度 1/2"', self.html)
        self.assertIn('width: 50%', self.html)


class ExpenseReportDesktopLayoutTest(TestCase):
    """费用报告页桌面两列布局回归锁

    本页是 rail-first（右列整块在 DOM 里排在主内容流之前）：这样移动端顺序
    （关键数字 → 三个图表 → 本月标签明细）与改造前逐块一致。
    顺带锁掉一件事：图表区不得再用 lg:grid-cols-2 —— 视口断点不跟随列宽，
    两列化后左列只有 864px，lg: 会把它硬拆成两个 416px 的图。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'activities' / 'expense_report.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        activity = Activity.objects.create(user=self.user, name='新西兰之旅')
        expense = Expense.objects.create(activity=activity, user=self.user,
                                         amount=Decimal('600'))
        apply_tags(expense, ['交通'])
        self.html = self.client.get('/activities/expense-report/').content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('月度趋势', '月度趋势图'), ('id="monthChart"', '趋势图画布'),
                  ('标签占比（近一年）', '饼图'), ('本月标签明细', '明细卡'),
                  ('id="mcList"', '明细列表挂载点')],
            right=[('关键数字', '概览卡标题'), ('本月合计', '本月数字')],
            mobile_order=['关键数字', '月度趋势', '本月标签明细'],
            rail_first=True)

    def test_chart_canvas_height_untouched(self):
        """三个图表仍各自带 220px 容器：Chart.js 的 responsive 靠父级定高"""
        for canvas in ('monthChart', 'tagChart', 'weekChart'):
            at = self.html.index('id="%s"' % canvas)
            self.assertIn('height:220px', self.html[at - 90:at],
                          f'{canvas} 的定高容器丢了，图会无限长高')




class ExpenseStatsAgentToolTest(TestCase):
    """费用专项统计工具：任意时间区间 + 按标签/活动维度汇总

    时间口径：付费日期 paid_at 在区间内（含边界）；未填 paid_at 的派生费用
    （AA 分账拆出等）按记录创建日归档，避免被静默漏计。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        self.user = User.objects.create_user('testuser', password='test')
        self.tool = get_tool('activities.expense_stats')
        self.activity = Activity.objects.create(user=self.user, name='桐庐周末游')
        self.other_activity = Activity.objects.create(user=self.user, name='北京出差')
        self.today = timezone.localdate()

    def _expense(self, amount, tags=None, paid_at=None, activity=None,
                 user=None, note=''):
        expense = Expense.objects.create(
            activity=activity or self.activity, user=user or self.user,
            amount=Decimal(amount), paid_at=paid_at, note=note)
        if tags:
            apply_tags(expense, tags)
        return expense

    def _total(self, **params):
        return self.tool['fn'](self.user, params)['card_data']['total']

    def test_default_covers_all_time(self):
        self._expense('100', paid_at=self.today - timedelta(days=400))
        self._expense('50', paid_at=self.today)
        self.assertEqual(self._total(), 150)

    def test_custom_date_range_is_inclusive(self):
        d1 = self.today - timedelta(days=10)
        d2 = self.today - timedelta(days=5)
        self._expense('100', paid_at=d1)          # 边界内（含）
        self._expense('40', paid_at=d2)           # 边界内（含）
        self._expense('60', paid_at=d1 - timedelta(days=1))   # 区间前
        self._expense('30', paid_at=d2 + timedelta(days=1))   # 区间后
        result = self.tool['fn'](self.user, {
            'date_from': d1.isoformat(), 'date_to': d2.isoformat()})
        self.assertEqual(result['card_data']['total'], 140)
        self.assertEqual(result['card_data']['count'], 2)
        self.assertIn(d1.isoformat(), result['reply'])

    def test_swapped_dates_are_normalized(self):
        d1 = self.today - timedelta(days=10)
        d2 = self.today - timedelta(days=5)
        self._expense('80', paid_at=d1)
        self._expense('20', paid_at=d2)
        swapped = self._total(date_from=d2.isoformat(), date_to=d1.isoformat())
        ordered = self._total(date_from=d1.isoformat(), date_to=d2.isoformat())
        self.assertEqual(swapped, ordered)

    def test_last_month_scope_excludes_this_month(self):
        """上个月口径：上限必须是上月最后一天——曾经 +31 天会溢出到本月 1 号"""
        first_of_month = self.today.replace(day=1)
        last_month_last = first_of_month - timedelta(days=1)
        last_month_first = last_month_last.replace(day=1)
        self._expense('100', paid_at=last_month_first + timedelta(days=2))
        self._expense('50', paid_at=last_month_last)      # 上月最后一天（含）
        self._expense('70', paid_at=first_of_month)       # 本月 1 号：不算
        result = self.tool['fn'](self.user, {'scope': 'last_month'})
        self.assertEqual(result['card_data']['total'], 150)
        self.assertIn('上个月', result['reply'])

    def test_week_scope_starts_on_monday(self):
        monday = self.today - timedelta(days=self.today.weekday())
        self._expense('100', paid_at=monday)
        self._expense('20', paid_at=monday - timedelta(days=1))   # 上周日
        self.assertEqual(self._total(scope='week'), 100)

    def test_last_30d_scope(self):
        self._expense('100', paid_at=self.today - timedelta(days=29))
        self._expense('50', paid_at=self.today - timedelta(days=30))   # 恰好界外
        self.assertEqual(self._total(scope='last_30d'), 100)

    def test_tag_breakdown_sorted_and_labeled(self):
        d = self.today
        self._expense('100', tags=['餐饮'], paid_at=d)
        self._expense('60', tags=['交通'], paid_at=d)
        self._expense('30', tags=['餐饮'], paid_at=d)
        card = self.tool['fn'](self.user, {'scope': 'month'})['card_data']
        tags = card['tags']
        self.assertEqual([t['label'] for t in tags], ['餐饮', '交通'])
        self.assertEqual(tags[0]['total'], 130)
        self.assertEqual(tags[0]['count'], 2)
        # 占比横条：按占总费用的比例（130/190 ≈ 68）
        self.assertEqual(tags[0]['pct'], 68)
        self.assertEqual(card['total'], 190)

    def test_activity_breakdown_top_with_links(self):
        d = self.today
        self._expense('100', paid_at=d, activity=self.activity)
        self._expense('500', paid_at=d, activity=self.other_activity)
        self._expense('40', paid_at=d, activity=self.activity)
        card = self.tool['fn'](self.user, {'scope': 'month'})['card_data']
        acts = card['activities']
        self.assertEqual(acts[0]['name'], '北京出差')
        self.assertEqual(acts[0]['total'], 500)
        self.assertIn(str(self.other_activity.id), acts[0]['detail_url'])
        self.assertEqual(len(acts), 2)

    def test_other_users_expenses_excluded(self):
        other = User.objects.create_user('someone', password='test')
        self._expense('999', paid_at=self.today, user=other)
        self.assertEqual(self._total(), 0)

    def test_null_paid_at_falls_back_to_created_at(self):
        """未填消费日期的费用按创建日归档（AA 分账拆出等派生记录不丢）"""
        self._expense('25', paid_at=None)
        self.assertEqual(self._total(scope='month'), 25)
        self.assertEqual(self._total(date_from='2020-01-01',
                                     date_to='2020-01-31'), 0)

    def test_empty_period_reply(self):
        result = self.tool['fn'](self.user, {'scope': 'month'})
        self.assertIn('没有费用记录', result['reply'])
        self.assertEqual(result['card_data']['total'], 0)

    def test_protocol_prompt_advertises_time_expressions(self):
        """协议里要写清时间范围的各种说法，模型才能把「上个月」换算成参数"""
        from core.agent_registry import build_protocol_prompt
        prompt = build_protocol_prompt()
        self.assertIn('expense_stats', prompt)
        self.assertIn('date_from + date_to', prompt)
        self.assertIn('last_month', prompt)


class UpdateParentAgentToolTest(TestCase):
    """activities.update 补齐父活动字段：预览 diff → 确认 → apply，环与歧义在预览挡住"""

    def setUp(self):
        from core.agent_registry import get_tool
        self.user = User.objects.create_user('testuser', password='test')
        self.tool = get_tool('activities.update')
        self.parent_a = Activity.objects.create(user=self.user, name='新疆大环线')
        self.parent_b = Activity.objects.create(user=self.user, name='青甘环线')
        self.child = Activity.objects.create(user=self.user, name='喀纳斯徒步')

    def test_preview_shows_parent_change_and_writes_nothing(self):
        preview = self.tool['fn'](self.user, {'target': '喀纳斯', 'parent': '新疆大环线'})
        self.assertEqual(preview['card'], 'confirm')
        change = next(c for c in preview['card_data']['changes'] if c['field'] == 'parent')
        self.assertEqual(change['old'], '空')
        self.assertEqual(change['new'], '新疆大环线')
        self.child.refresh_from_db()
        self.assertIsNone(self.child.parent)

    def test_apply_moves_under_parent(self):
        self.tool['apply'](self.user, {'target_id': self.child.id, 'parent': '新疆大环线'})
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent, self.parent_a)
        log = ActivityLog.objects.filter(activity=self.child, action='edited') \
            .order_by('-id').first()
        self.assertIn('父活动', log.summary)
        self.assertIn('新疆大环线', log.summary)

    def test_apply_reassigns_between_parents(self):
        self.child.parent = self.parent_b
        self.child.save()
        self.tool['apply'](self.user, {'target_id': self.child.id, 'parent': '新疆大环线'})
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent, self.parent_a)

    def test_clear_parent_with_empty_or_none(self):
        self.child.parent = self.parent_a
        self.child.save()
        self.tool['apply'](self.user, {'target_id': self.child.id, 'parent': ''})
        self.child.refresh_from_db()
        self.assertIsNone(self.child.parent)
        self.tool['apply'](self.user, {'target_id': self.child.id, 'parent': 'none'})
        self.child.refresh_from_db()
        self.assertIsNone(self.child.parent)

    def test_self_as_parent_is_rejected(self):
        with self.assertRaises(ToolError):
            self.tool['fn'](self.user, {'target': '喀纳斯', 'parent': '喀纳斯'})

    def test_cycle_is_rejected(self):
        """父活动不能挂到自己的（孙）子活动下面，否则父子关系成环"""
        self.child.parent = self.parent_a
        self.child.save()
        with self.assertRaises(ToolError) as ctx:
            self.tool['fn'](self.user, {'target': '新疆大环线', 'parent': '喀纳斯'})
        self.assertIn('不能反向挂为父活动', str(ctx.exception))

    def test_ambiguous_parent_raises_candidates(self):
        Activity.objects.create(user=self.user, name='环线加购')
        with self.assertRaises(CandidateToolError) as ctx:
            self.tool['fn'](self.user, {'target': '喀纳斯', 'parent': '环线'})
        self.assertTrue([c['name'] for c in ctx.exception.candidates])

    def test_missing_parent_name_raises_tool_error(self):
        with self.assertRaises(ToolError):
            self.tool['fn'](self.user, {'target': '喀纳斯', 'parent': '不存在的活动'})

    def test_other_users_activity_cannot_be_parent(self):
        other = User.objects.create_user('someone', password='test')
        Activity.objects.create(user=other, name='别人的活动')
        with self.assertRaises(ToolError):
            self.tool['fn'](self.user, {'target': '喀纳斯', 'parent': '别人的活动'})

    def test_unrelated_fields_untouched(self):
        self.child.start_date = date(2026, 9, 20)
        self.child.save()
        self.tool['apply'](self.user, {'target_id': self.child.id, 'parent': '新疆大环线'})
        self.child.refresh_from_db()
        self.assertEqual(self.child.start_date, date(2026, 9, 20))

    def test_prompt_advertises_parent_capability(self):
        """协议里要写清 parent 参数与「移出父活动」的说法，模型才会用"""
        from core.agent_registry import build_protocol_prompt
        prompt = build_protocol_prompt()
        self.assertIn('parent（父活动名称关键词', prompt)
        self.assertIn('移出父活动', prompt)
        self.assertIn('全部可编辑字段', prompt)


class CategoryToTagsMigrationTest(TransactionTestCase):
    """0017 数据搬迁回归锁：分类体系下线时，存量数据无损转入标签体系

    真实执行迁移文件里的 migrate_category_to_tags：用 MigrationLoader
    重放 0016 时点的 historical models（ExpenseCategory / 带 category 列
    的 Expense），在测试库临时重建历史结构并灌入存量数据。建表/加列是
    DDL，SQLite 不允许在 TestCase 的事务内做，因此本类用
    TransactionTestCase（无外层事务，靠 tearDown 显式清理 + 结束后
    flush 兜底）。锁住四件事：
    类别 → scope='expense' 标签一一对应、同名标签复用不重建、
    费用 → tags 关联无遗漏无重复、重复执行幂等。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import importlib
        from django.db.migrations.loader import MigrationLoader
        # ignore_no_migrations=True：不查 django_migrations 表，纯磁盘迁移图
        loader = MigrationLoader(None, ignore_no_migrations=True)
        cls.historical_apps = loader.project_state(
            [('activities', '0016_migrate_taggit_data')], at_end=True).apps
        cls.migrate_fn = staticmethod(importlib.import_module(
            'activities.migrations.0017_delete_expensecategory_and_more'
        ).migrate_category_to_tags)

    def setUp(self):
        from django.db import connection
        self.ExpenseCategory = self.historical_apps.get_model(
            'activities', 'ExpenseCategory')
        self.HistoricalExpense = self.historical_apps.get_model(
            'activities', 'Expense')
        with connection.schema_editor() as editor:
            editor.create_model(self.ExpenseCategory)
            editor.add_field(self.HistoricalExpense,
                             self.HistoricalExpense._meta.get_field('category'))

    def tearDown(self):
        from django.db import connection
        # add_field 会因 0016 时点 Meta 连带重建含 category 的旧复合索引
        # （0009 的 user+category+-paid_at），SQLite 无法在 DROP COLUMN 时
        # 自动摘除它，必须先手动删掉
        with connection.cursor() as cursor:
            cursor.execute('DROP INDEX IF EXISTS activities__user_id_a1454e_idx')
        with connection.schema_editor() as editor:
            editor.remove_field(self.HistoricalExpense,
                                self.HistoricalExpense._meta.get_field('category'))
            editor.delete_model(self.ExpenseCategory)

    def test_migration_converts_categories_to_tags(self):
        """存量类别 → scope='expense' 标签、存量费用 → tags 关联，一一对应

        测试库走完整迁移链后，0014 种子的 9 类已被 0017 转为 expense 标签
        （RunPython 对空 Expense 表只建标签不挂费用）；这里重建 0016 时点
        结构灌入存量费用，验证 0017 的四个口径：同名标签复用、已挂费用
        去重、空类别/未知 key 跳过、重复执行幂等。
        """
        user = User.objects.create_user('testuser', password='test')
        activity = Activity.objects.create(user=user, name='迁移演练')

        base_names = set(Tag.objects.filter(scope='expense')
                         .values_list('name', flat=True))
        self.assertIn('餐饮', base_names)
        self.assertIn('交通', base_names)

        # 存量类别（模拟 0016 时点的 ExpenseCategory 表）
        self.ExpenseCategory.objects.create(key='food', label='餐饮', sort=30)
        self.ExpenseCategory.objects.create(key='transport', label='交通', sort=10)

        # historical FK 与真实模型是不同类，传主键绕过实例类型校验
        HistoricalExpense = self.HistoricalExpense
        e_food = HistoricalExpense.objects.create(
            activity_id=activity.id, user_id=user.id,
            amount=Decimal('200'), category='food')
        e_dup = HistoricalExpense.objects.create(
            activity_id=activity.id, user_id=user.id,
            amount=Decimal('60'), category='transport')
        # historical 模型不被 core.tags 的宿主注册表识别，模拟「已挂过」
        # 直接写 through 表：迁移不得重复挂
        tag = Tag.objects.get(scope='expense', name='交通')
        HistoricalExpense.tags.through.objects.create(
            expense_id=e_dup.pk, tag_id=tag.pk)
        e_nocat = HistoricalExpense.objects.create(
            activity_id=activity.id, user_id=user.id,
            amount=Decimal('10'), category='')
        e_orphan = HistoricalExpense.objects.create(
            activity_id=activity.id, user_id=user.id,
            amount=Decimal('5'), category='ghost')   # 脏数据：key 不在类别表，跳过不炸

        self.migrate_fn(self.historical_apps, None)

        # 类别 → 标签：同名复用不重建（迁移前后标签总数不变）
        self.assertEqual(
            Tag.objects.filter(scope='expense').count(), len(base_names))
        self.assertEqual(
            Tag.objects.filter(scope='expense', name='餐饮').count(), 1)
        self.assertEqual(
            Tag.objects.filter(scope='expense', name='交通').count(), 1)
        # 费用 → tags：key 反查 label 一一挂上
        self.assertEqual(set(tag_names(e_food)), {'餐饮'})
        self.assertEqual(set(tag_names(e_dup)), {'交通'})   # 去重
        self.assertEqual(tag_names(e_nocat), [])            # 空类别不挂
        self.assertEqual(tag_names(e_orphan), [])           # 未知 key 不挂
        # 幂等：重复执行不产生重复关联或重复标签
        self.migrate_fn(self.historical_apps, None)
        self.assertEqual(set(tag_names(e_food)), {'餐饮'})
        self.assertEqual(
            Tag.objects.filter(scope='expense').count(), len(base_names))


class ExpenseTagChipsTest(TestCase):
    """详情页费用表单「常用标签」chips：used_tags 口径 + 频次排序 + 上限 8

    点击填充是纯前端（appendExpenseTag 追加不覆盖），这里锁视图注入与
    渲染口径：只出现当前用户用过的 expense 标签，频次降序，最多 8 个。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='标签chips活动')

    def _expense(self, tags, activity=None, user=None):
        e = Expense.objects.create(activity=activity or self.activity,
                                   user=user or self.user, amount=10)
        apply_tags(e, tags)
        return e

    def test_chips_sorted_by_frequency_and_rendered(self):
        """chips 渲染为 onclick 按钮，按使用频次降序（多者在前）"""
        for _ in range(3):
            self._expense(['餐饮'])
        self._expense(['交通'])
        html = self.client.get(f'/activities/{self.activity.id}/').content.decode()
        self.assertIn('id="expense-tag-chips"', html)
        self.assertLess(html.find("appendExpenseTag('餐饮')"),
                        html.find("appendExpenseTag('交通')"))

    def test_chips_only_current_user_tags(self):
        """别人的费用标签不进我的 chips（used_tags 按用户可见范围过滤）"""
        self._expense(['餐饮'])
        other = User.objects.create_user('other', password='test')
        other_activity = Activity.objects.create(user=other, name='别人的活动')
        self._expense(['别人的标签'], activity=other_activity, user=other)
        html = self.client.get(f'/activities/{self.activity.id}/').content.decode()
        self.assertIn("appendExpenseTag('餐饮')", html)
        # 只断 chips 按钮（页面 datalist 的全局建议含预建启用标签，属预期）
        self.assertNotIn("appendExpenseTag('别人的标签')", html)

    def test_chips_capped_at_8(self):
        """超过 8 个只取频次前 8，避免挤压表单"""
        for i in range(10):
            self._expense([f'标签{i}'])
        html = self.client.get(f'/activities/{self.activity.id}/').content.decode()
        self.assertEqual(html.count('onclick="appendExpenseTag('), 8)

    def test_chips_absent_without_history(self):
        """从未用过费用标签时整块不渲染"""
        html = self.client.get(f'/activities/{self.activity.id}/').content.decode()
        self.assertNotIn('expense-tag-chips', html)


class ActivityTagEditRegressionTest(TestCase):
    """编辑/创建活动表单更新标签的回归锁（2026-09 taggit→core.Tag 迁移踩坑）

    ModelForm._save_m2m 只认模型 M2M 字段名：ActivityForm 把 tags 换成
    PlainTagField 后，form.save() / form.save_m2m() 会把名字字符串直接传给
    instance.tags.set() 逐字符当主键 → ValueError 500。修复后由
    PlainTagFormMixin 摘除，tags 落库统一走视图层 apply_tags。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.activity = Activity.objects.create(
            user=self.user, name='标签回归活动', status='planned')
        apply_tags(self.activity, ['旧标签'])

    def _edit_payload(self, **overrides):
        payload = {
            'name': '标签回归活动', 'description': '',
            'start_date': '2026-09-09', 'end_date': '', 'status': 'planned',
            'parent': '', 'participants_input': '', 'new_children': '',
        }
        payload.update(overrides)
        return payload

    def test_edit_with_tags_persists(self):
        """编辑页提交带标签：302 + 标签整体替换落库（曾是 500）"""
        resp = self.client.post(f'/activities/{self.activity.id}/edit/',
                                self._edit_payload(tags='新标签甲, 新标签乙'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(set(tag_names(self.activity)), {'新标签甲', '新标签乙'})
        # 旧标签被整体替换（set 语义），不残留
        self.assertNotIn('旧标签', tag_names(self.activity))

    def test_edit_with_empty_tags_clears(self):
        """编辑页清空标签输入提交：标签全部移除而非报错"""
        resp = self.client.post(f'/activities/{self.activity.id}/edit/',
                                self._edit_payload(tags=''))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(tag_names(self.activity), [])

    def test_edit_page_renders_current_tags(self):
        """编辑页 GET：当前标签回显到表单（prepare_value 链路）"""
        resp = self.client.get(f'/activities/{self.activity.id}/edit/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('旧标签', resp.content.decode())

    def test_create_with_tags_via_form_save_m2m(self):
        """新建活动带标签：form.save_m2m() 不再拿字符串炸 M2M（曾是同族雷）"""
        resp = self.client.post('/activities/new/', {
            'name': '带标签新活动', 'description': '',
            'start_date': '2026-09-10', 'end_date': '', 'status': 'planned',
            'parent': '', 'participants_input': '', 'new_children': '',
            'tags': '团建, 出行',
        })
        self.assertEqual(resp.status_code, 302)
        activity = Activity.objects.get(user=self.user, name='带标签新活动')
        self.assertEqual(set(tag_names(activity)), {'团建', '出行'})


class CalendarFeedTest(TestCase):
    """ICS 日历订阅：token 鉴权、字段映射、令牌生命周期（2026-09-11）"""

    def setUp(self):
        self.user = User.objects.create_user('caluser', password='x')
        self.other = User.objects.create_user('calother', password='x')
        self.feed = CalendarFeed.issue(self.user)
        self.today = timezone.localdate()

    def _mk(self, **kw):
        defaults = dict(user=self.user, name='测试活动', status='planned',
                        start_date=self.today, end_date=self.today + timedelta(days=2))
        defaults.update(kw)
        return Activity.objects.create(**defaults)

    def _feed_url(self, token=None):
        return reverse('calendar_feed_ics', args=[token or self.feed.token])

    def test_feed_requires_no_login_and_valid_token(self):
        """未登录持有效 token 可拉取（Apple 日历服务器无会话）"""
        self._mk()
        resp = self.client.get(self._feed_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/calendar; charset=utf-8')

    def test_bad_or_revoked_token_404(self):
        """错误 token 与吊销 token 一律 404，不泄露存在性"""
        self.assertEqual(self.client.get(self._feed_url('wrong-token')).status_code, 404)
        self.feed.revoke()
        self.assertEqual(self.client.get(self._feed_url()).status_code, 404)

    def test_feed_isolated_per_user(self):
        """feed 只含归属用户的活动"""
        self._mk(name='我的活动')
        Activity.objects.create(user=self.other, name='别人的活动', status='planned',
                                start_date=self.today, end_date=self.today)
        body = self.client.get(self._feed_url()).content.decode()
        self.assertIn('我的活动', body)
        self.assertNotIn('别人的活动', body)

    def test_export_scope_and_mapping(self):
        """planned/in_progress 且有日期的导出；done/cancelled/无日期不导出；DTEND 排他"""
        self._mk(name='进行中活动', status='in_progress')
        self._mk(name='已完成活动', status='done')
        self._mk(name='已取消活动', status='cancelled')
        self._mk(name='无日期活动', start_date=None, end_date=None)
        self._mk(name='已过期活动', start_date=self.today - timedelta(days=30),
                 end_date=self.today - timedelta(days=20))

        body = self.client.get(self._feed_url()).content.decode()
        self.assertIn('BEGIN:VCALENDAR', body)
        self.assertIn('进行中活动', body)
        self.assertNotIn('已完成活动', body)
        self.assertNotIn('已取消活动', body)
        self.assertNotIn('无日期活动', body)
        self.assertNotIn('已过期活动', body)
        # DTSTART 为 DATE 值；DTEND = end_date + 1 天（RFC 5545 排他）
        d = self.today.strftime('%Y%m%d')
        self.assertIn(f'DTSTART;VALUE=DATE:{d}', body)
        self.assertIn(f'DTEND;VALUE=DATE:{(self.today + timedelta(days=3)).strftime("%Y%m%d")}', body)
        # 状态映射与提醒
        self.assertIn('STATUS:CONFIRMED', body)
        self.assertIn('BEGIN:VALARM', body)
        self.assertIn('TRIGGER:-P1D', body)
        # UID 稳定 + 详情链接
        self.assertIn('UID:activity-', body)
        self.assertIn('/activities/', body)

    def test_timed_activity_exports_datetime_utc(self):
        """带时间活动导出 DATE-TIME（UTC Z 后缀），Apple 日历准点提醒而非全天

        2026-09-17：本地 14:00（Asia/Shanghai）应导出为 06:00Z；
        定点事件不再出现 VALUE=DATE 行，提醒从提前 1 天改为提前 30 分钟。
        """
        from datetime import datetime, time as dt_time, timezone as dt_timezone
        self._mk(name='定点活动', start_date=self.today, end_date=self.today,
                 start_time=dt_time(14, 0), end_time=dt_time(16, 0))
        body = self.client.get(self._feed_url()).content.decode()

        def utc(h, m):
            aware = timezone.make_aware(datetime.combine(self.today, dt_time(h, m)))
            return aware.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')

        self.assertIn(f'DTSTART:{utc(14, 0)}', body)
        self.assertIn(f'DTEND:{utc(16, 0)}', body)
        self.assertNotIn('DTSTART;VALUE=DATE', body)
        self.assertIn('TRIGGER:-PT30M', body)
        self.assertNotIn('TRIGGER:-P1D', body)

    def test_timed_end_time_defaults_and_inverted_fallback(self):
        """只填开始时间 → 跨度+1 小时收尾；结束时间早于开始 → 兜底 1 小时"""
        from datetime import datetime, time as dt_time, timezone as dt_timezone

        def utc(h, m):
            aware = timezone.make_aware(datetime.combine(self.today, dt_time(h, m)))
            return aware.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')

        self._mk(name='只有开始时间', start_date=self.today, end_date=self.today,
                 start_time=dt_time(9, 0), end_time=None)
        body = self.client.get(self._feed_url()).content.decode()
        self.assertIn(f'DTSTART:{utc(9, 0)}', body)
        self.assertIn(f'DTEND:{utc(10, 0)}', body)

        Activity.objects.all().delete()
        self._mk(name='时间倒挂', start_date=self.today, end_date=self.today,
                 start_time=dt_time(18, 0), end_time=dt_time(8, 0))
        body = self.client.get(self._feed_url()).content.decode()
        self.assertIn(f'DTSTART:{utc(18, 0)}', body)
        self.assertIn(f'DTEND:{utc(19, 0)}', body)

    def test_all_day_and_timed_coexist(self):
        """纯日期活动维持全天导出，带时间活动走 DATE-TIME，两种模式共存"""
        from datetime import time as dt_time
        self._mk(name='全天活动')
        self._mk(name='定点活动', start_date=self.today, end_date=self.today,
                 start_time=dt_time(14, 0))
        body = self.client.get(self._feed_url()).content.decode()
        d = self.today.strftime('%Y%m%d')
        # 全天：DATE 值 + 提前 1 天
        self.assertIn(f'DTSTART;VALUE=DATE:{d}', body)
        self.assertIn('TRIGGER:-P1D', body)
        # 定点：DATE-TIME 值 + 提前 30 分钟
        self.assertIn('DTSTART:2', body)
        self.assertIn('TRIGGER:-PT30M', body)
        self.assertIn('全天活动', body)
        self.assertIn('定点活动', body)

    def test_subtask_exported(self):
        """符合条件的子任务也进日历，描述里带父活动名"""
        parent = self._mk(name='父活动')
        child = Activity.objects.create(user=self.user, name='子任务', parent=parent,
                                        status='planned', start_date=self.today,
                                        end_date=self.today)
        body = self.client.get(self._feed_url()).content.decode()
        self.assertIn('子任务', body)
        self.assertIn('父活动: 父活动', body)

    def test_escaping_and_line_folding(self):
        """TEXT 特殊字符转义 + 75 字节行折叠（不拆多字节）"""
        self._mk(name='含逗号,分号;反斜杠\\换行\n的名字', description='很长' * 200)
        body = self.client.get(self._feed_url()).content.decode()
        self.assertIn('含逗号\\,分号\\;反斜杠\\\\换行\\n的名字', body)
        for line in body.split('\r\n'):
            self.assertLessEqual(len(line.encode('utf-8')), 75)
        # 折叠行解开后的还原完整性
        unfolded = body.replace('\r\n ', '')
        self.assertIn('很长' * 100, unfolded)

    def test_settings_page_and_lifecycle(self):
        """设置页登录可见；重新生成旧 token 失效；吊销后恢复可用"""
        # 匿名访问设置页 → 跳登录
        resp = self.client.get('/activities/calendar/feed-settings/')
        self.assertEqual(resp.status_code, 302)

        self.client.force_login(self.user)
        resp = self.client.get('/activities/calendar/feed-settings/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.feed.token, resp.content.decode())

        # 重新生成：旧 token 404，新 token 200
        old_token = self.feed.token
        self.client.post('/activities/calendar/feed-settings/', {'action': 'regenerate'})
        self.feed.refresh_from_db()
        self.assertNotEqual(self.feed.token, old_token)
        self.assertEqual(self.client.get(self._feed_url(old_token)).status_code, 404)
        self.assertEqual(self.client.get(self._feed_url()).status_code, 200)

        # 吊销 → 404；重新生成 → 解除吊销恢复 200
        self.client.post('/activities/calendar/feed-settings/', {'action': 'revoke'})
        self.feed.refresh_from_db()
        self.assertIsNotNone(self.feed.revoked_at)
        self.assertEqual(self.client.get(self._feed_url()).status_code, 404)
        self.client.post('/activities/calendar/feed-settings/', {'action': 'regenerate'})
        self.feed.refresh_from_db()
        self.assertIsNone(self.feed.revoked_at)
        self.assertEqual(self.client.get(self._feed_url()).status_code, 200)

    def test_issue_idempotent(self):
        """issue 幂等：同一用户重复签发返回同一条令牌"""
        again = CalendarFeed.issue(self.user)
        self.assertEqual(again.pk, self.feed.pk)


class ActivityListPaginationTest(TestCase):
    """活动列表分页：按顶级活动分页、子活动跟随父活动同页、非法页码回退（2026-09-13）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def _create_tops(self, n, status='planned'):
        """创建 n 个顶级活动，start_date 随序号递减且全部在未来（避免被
        start_due_activities 自动转为 in_progress），保证默认排序（start_date 倒序）稳定"""
        base = timezone.localdate()
        return [
            Activity.objects.create(
                user=self.user, name=f'活动{i:02d}', status=status,
                start_date=base + timedelta(days=n - i))
            for i in range(1, n + 1)
        ]

    def _top_level_count(self, response):
        """响应上下文中当前页顶级活动数（depth==0），子活动不计入"""
        return sum(1 for a in response.context['activities'] if not a.depth)

    def test_first_and_second_page_slice_top_level(self):
        """首页 20 个顶级活动，第 2 页余下 5 个；页码信息正确"""
        self._create_tops(25)
        r1 = self.client.get('/activities/')
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.context['page_obj'].number, 1)
        self.assertEqual(self._top_level_count(r1), 20)
        c1 = r1.content.decode()
        self.assertIn('活动01', c1)
        self.assertIn('活动20', c1)
        self.assertNotIn('活动21', c1)

        r2 = self.client.get('/activities/?page=2')
        self.assertEqual(r2.context['page_obj'].number, 2)
        self.assertEqual(self._top_level_count(r2), 5)
        c2 = r2.content.decode()
        self.assertIn('活动21', c2)
        self.assertIn('活动25', c2)
        self.assertNotIn('活动20', c2)
        self.assertIn('共 25 条 · 第 2/2 页', c2)

    def test_children_follow_parent_and_not_counted(self):
        """子活动跟随父活动出现在同一页，不跨页断裂、不计入每页条数"""
        tops = self._create_tops(25)
        child = Activity.objects.create(user=self.user, name='边界子任务',
                                        parent=tops[-1])
        r1 = self.client.get('/activities/')
        self.assertEqual(self._top_level_count(r1), 20)
        self.assertNotIn('边界子任务', r1.content.decode())  # 子活动留在父活动所在页

        r2 = self.client.get('/activities/?page=2')
        self.assertEqual(self._top_level_count(r2), 5)  # 子活动不计入
        c2 = r2.content.decode()
        self.assertIn('活动25', c2)
        self.assertIn('边界子任务', c2)  # 与父活动同页

    def test_invalid_or_out_of_range_page_falls_back_to_first(self):
        """page=abc / 越界 / 负数 / 小数均静默回退第 1 页"""
        self._create_tops(3)
        for bad in ('abc', '999', '-1', '1.5', ' '):
            r = self.client.get(f'/activities/?page={bad}')
            self.assertEqual(r.status_code, 200, f'page={bad!r} 不应报错')
            self.assertEqual(r.context['page_obj'].number, 1, f'page={bad!r} 应回退第 1 页')
            self.assertIn('活动01', r.content.decode())

    def test_pagination_links_preserve_filters_and_sort(self):
        """翻页链接携带全部筛选/排序参数；筛选结果跨页时条件不丢失"""
        self._create_tops(25)  # 全部 planned
        done = Activity.objects.create(user=self.user, name='已完成活动', status='done')
        r = self.client.get('/activities/?status=planned')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['page_obj'].number, 1)
        content = r.content.decode()
        self.assertNotIn('已完成活动', content)      # 筛选仍生效
        self.assertIn('?status=planned&page=2', content)  # 翻页链接带筛选

        r2 = self.client.get('/activities/?status=planned&page=2')
        self.assertEqual(r2.context['page_obj'].number, 2)
        self.assertNotIn('已完成活动', r2.content.decode())

        # 排序参数也保留在翻页链接里
        r3 = self.client.get('/activities/?sort=-cost')
        self.assertIn('sort=-cost', r3.context['page_base_qs'])
        self.assertIn('sort=-cost&page=2', r3.content.decode())

    def test_empty_list_and_single_page_render_without_pagination(self):
        """空列表不报错；不足一页时不渲染分页控件"""
        r0 = self.client.get('/activities/')
        self.assertEqual(r0.status_code, 200)
        self.assertIn('还没有活动记录', r0.content.decode())
        self.assertNotIn('page=2', r0.content.decode())

        self._create_tops(3)
        r1 = self.client.get('/activities/')
        self.assertEqual(r1.context['page_obj'].paginator.num_pages, 1)
        self.assertNotIn('page=2', r1.content.decode())

    def test_page_window_collapses_on_many_pages(self):
        """总页数多时页码收敛为 当前页 ±2 + 首末页（省略号占位）"""
        self._create_tops(45)  # 3 页
        r = self.client.get('/activities/')
        self.assertEqual(r.context['page_numbers'], [1, 2, 3])
        self.assertNotIn('select-none">…</span>', r.content.decode())


# ==================== 活动评论 ====================

class ActivityCommentTest(TestCase):
    """活动评论：详情页添加/删除 + 可见性跟随活动 + AI 工具读写"""

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.superuser = User.objects.create_superuser('admin', password='test')
        self.activity = Activity.objects.create(user=self.user, name='新西兰之旅')
        self.client.login(username='raven', password='test')

    def _add(self, activity_id=None, content='追加一条评论'):
        return self.client.post(
            reverse('activities:activity_comment_add',
                    args=[activity_id or self.activity.id]),
            {'content': content})

    def test_add_comment_renders_in_detail_and_logs(self):
        r = self._add(content='记得带冲锋衣')
        self.assertRedirects(r, reverse('activities:activity_detail', args=[self.activity.id]))
        comment = self.activity.comments.get()
        self.assertEqual(comment.user, self.user)
        self.assertEqual(comment.content, '记得带冲锋衣')
        # 写操作必须留痕：commented 日志
        self.assertTrue(ActivityLog.objects.filter(
            activity=self.activity, action='commented').exists())
        # 详情页渲染评论
        html = self.client.get(reverse('activities:activity_detail',
                                       args=[self.activity.id])).content.decode()
        self.assertIn('记得带冲锋衣', html)
        self.assertIn('评论（1）', html)

    def test_add_empty_content_rejected(self):
        self._add(content='   ')
        self.assertFalse(self.activity.comments.exists())

    def test_comment_ordering_chronological(self):
        from activities.models import ActivityComment
        c1 = ActivityComment.objects.create(activity=self.activity, user=self.user, content='第一条')
        c2 = ActivityComment.objects.create(activity=self.activity, user=self.user, content='第二条')
        self.assertEqual(list(self.activity.comments.all()), [c1, c2])

    def test_author_can_delete_own_comment(self):
        from activities.models import ActivityComment
        c = ActivityComment.objects.create(activity=self.activity, user=self.user, content='x')
        r = self.client.post(reverse('activities:activity_comment_delete', args=[c.id]))
        self.assertRedirects(r, reverse('activities:activity_detail', args=[self.activity.id]))
        self.assertFalse(ActivityComment.objects.filter(id=c.id).exists())

    def test_non_author_cannot_delete(self):
        from activities.models import ActivityComment
        c = ActivityComment.objects.create(activity=self.activity, user=self.user, content='x')
        self.client.logout()
        self.client.login(username='other', password='test')
        self.client.post(reverse('activities:activity_comment_delete', args=[c.id]))
        self.assertTrue(ActivityComment.objects.filter(id=c.id).exists())

    def test_superuser_can_delete_others_comment(self):
        from activities.models import ActivityComment
        c = ActivityComment.objects.create(activity=self.activity, user=self.user, content='x')
        self.client.logout()
        self.client.login(username='admin', password='test')
        self.client.post(reverse('activities:activity_comment_delete', args=[c.id]))
        self.assertFalse(ActivityComment.objects.filter(id=c.id).exists())

    def test_other_user_cannot_comment_on_invisible_activity(self):
        """数据可见性：用户 B 无法评论用户 A 的活动（get_visible → 404）"""
        self.client.logout()
        self.client.login(username='other', password='test')
        r = self._add()
        self.assertEqual(r.status_code, 404)
        self.assertFalse(self.activity.comments.exists())

    def test_add_requires_post(self):
        r = self.client.get(reverse('activities:activity_comment_add', args=[self.activity.id]))
        self.assertEqual(r.status_code, 405)


class ActivityCommentAgentToolTest(TestCase):
    """AI 读写评论：activities.add_comment 写入 + activities.get/comments 读取"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='周末游')
        from core.agent_registry import get_tool
        self.get_tool = get_tool

    def test_add_comment_tool_creates_and_logs(self):
        tool = self.get_tool('activities.add_comment')
        result = tool['fn'](self.user, {'target': '周末游', 'content': '改期到下周六'})
        self.assertTrue(result['changed'])
        comment = self.activity.comments.get()
        self.assertEqual(comment.content, '改期到下周六')
        self.assertTrue(ActivityLog.objects.filter(
            activity=self.activity, action='commented', summary__contains='改期').exists())
        self.assertIn('周末游', result['reply'])

    def test_add_comment_tool_requires_content(self):
        tool = self.get_tool('activities.add_comment')
        with self.assertRaises(ToolError):
            tool['fn'](self.user, {'target': '周末游', 'content': '  '})

    def test_comments_tool_reads_all(self):
        from activities.models import ActivityComment
        ActivityComment.objects.create(activity=self.activity, user=self.user, content='带帐篷')
        ActivityComment.objects.create(activity=self.activity, user=self.user, content='查天气')
        tool = self.get_tool('activities.comments')
        result = tool['fn'](self.user, {'target': '周末游'})
        self.assertIn('2 条评论', result['reply'])
        self.assertIn('带帐篷', result['reply'])
        self.assertIn('查天气', result['reply'])
        self.assertEqual(result['activity_ids'], [self.activity.id])

    def test_comments_tool_empty(self):
        tool = self.get_tool('activities.comments')
        result = tool['fn'](self.user, {'target': '周末游'})
        self.assertIn('还没有评论', result['reply'])

    def test_get_tool_card_includes_comments(self):
        """AI 决策读评论的主通道：activities.get 的 card_data 快照带评论"""
        from activities.models import ActivityComment
        ActivityComment.objects.create(activity=self.activity, user=self.user, content='优先订机票')
        result = self.get_tool('activities.get')['fn'](self.user, {'target': '周末游'})
        card = result['card_data']
        self.assertEqual(card['comments_count'], 1)
        self.assertEqual(card['comments'][0]['content'], '优先订机票')
        self.assertEqual(card['comments'][0]['author'], 'testuser')

    def test_other_user_tool_cannot_touch_invisible_activity(self):
        other = User.objects.create_user('other', password='test')
        tool = self.get_tool('activities.add_comment')
        with self.assertRaises(Exception):
            tool['fn'](other, {'target': '周末游', 'content': '越权'})
        self.assertFalse(self.activity.comments.exists())


class BlockedByEditTest(TestCase):
    """详情页前置依赖手动配置：搜索 / 添加（环检测）/ 移除，均为 JSON 端点"""

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.activity = Activity.objects.create(user=self.user, name='意大利旅游')
        self.client.login(username='raven', password='test')

    def _search(self, q=''):
        url = reverse('activities:blocked_search', args=[self.activity.id])
        return self.client.get(url + ('?q=' + q if q else ''))

    def _add(self, dep_id, activity=None):
        target = activity or self.activity
        return self.client.post(
            reverse('activities:blocked_add', args=[target.id]),
            data=json.dumps({'activity_id': dep_id}), content_type='application/json')

    def _remove(self, dep_id):
        return self.client.post(
            reverse('activities:blocked_remove', args=[self.activity.id, dep_id]))

    def test_search_excludes_self_and_linked(self):
        visa = Activity.objects.create(user=self.user, name='办理签证')
        hotel = Activity.objects.create(user=self.user, name='订酒店')
        self.activity.blocked_by.add(visa)
        data = self._search().json()
        ids = {i['id'] for i in data['items']}
        self.assertNotIn(self.activity.id, ids)      # 排除自己
        self.assertNotIn(visa.id, ids)               # 排除已添加的
        self.assertIn(hotel.id, ids)

    def test_search_q_filters_by_name(self):
        Activity.objects.create(user=self.user, name='办理签证')
        Activity.objects.create(user=self.user, name='订酒店')
        data = self._search('签证').json()
        self.assertEqual([i['name'] for i in data['items']], ['办理签证'])

    def test_add_success_with_log(self):
        visa = Activity.objects.create(user=self.user, name='办理签证')
        resp = self._add(visa.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        self.assertIn(visa, self.activity.blocked_by.all())
        self.assertTrue(ActivityLog.objects.filter(
            activity=self.activity, action='edited',
            summary__contains='办理签证').exists())

    def test_add_rejects_self_duplicate_and_cycle(self):
        a = Activity.objects.create(user=self.user, name='A')
        b = Activity.objects.create(user=self.user, name='B')
        a.blocked_by.add(b)   # a 的前置是 b → 再给 b 添加 a 就成环
        self.assertEqual(self._add(self.activity.id).status_code, 400)  # 依赖自己
        self._add(a.id)
        self.assertEqual(self._add(a.id).status_code, 400)              # 重复添加
        resp = self._add(a.id, activity=b)                               # b←a 成环
        self.assertEqual(resp.status_code, 400)
        self.assertIn('循环', resp.json()['error'])
        self.assertFalse(b.blocked_by.filter(id=a.id).exists())

    def test_add_foreign_activity_returns_json_404(self):
        foreign = Activity.objects.create(user=self.other, name='别人的活动')
        resp = self._add(foreign.id)
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())

    def test_foreign_user_on_target_activity_gets_json_404(self):
        visa = Activity.objects.create(user=self.user, name='办理签证')
        self.client.login(username='other', password='test')
        resp = self._add(visa.id)
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())
        self.assertFalse(self.activity.blocked_by.exists())

    def test_remove_success_then_404(self):
        visa = Activity.objects.create(user=self.user, name='办理签证')
        self.activity.blocked_by.add(visa)
        resp = self._remove(visa.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.activity.blocked_by.exists())
        self.assertTrue(ActivityLog.objects.filter(
            activity=self.activity, action='edited',
            summary__contains='移除前置依赖').exists())
        self.assertEqual(self._remove(visa.id).status_code, 404)

    def test_detail_page_renders_dep_section(self):
        visa = Activity.objects.create(user=self.user, name='办理签证', status='in_progress')
        self.activity.blocked_by.add(visa)
        html = self.client.get(reverse(
            'activities:activity_detail', args=[self.activity.id])).content.decode()
        # 计数渲染在 <span id="dep-count"> 内，不能按纯文本「前置依赖（1）」断言
        self.assertIn('前置依赖（', html)
        self.assertIn('<span id="dep-count">1</span>', html)
        self.assertIn('办理签证', html)
        self.assertIn('搜索活动，添加为前置依赖', html)


class CsvExportTest(TestCase):
    """⑦ CSV 导出：BOM / 可见性隔离 / 字段齐全（数据主权兜底，可直接进 Excel）"""

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.activity = Activity.objects.create(
            user=self.user, name='意大利旅游', status='in_progress',
            description='六天五夜')
        apply_tags(self.activity, ['旅行'])
        Expense.objects.create(
            activity=self.activity, user=self.user, amount=Decimal('120.50'),
            paid_at=timezone.localdate(), note='机票')
        self.client.login(username='raven', password='test')

    def test_activity_export_contains_bom_and_fields(self):
        resp = self.client.get(reverse('activities:activity_export'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        # BOM 必须在：Excel 打开无 BOM 的 UTF-8 中文 CSV 全是乱码
        self.assertTrue(body.startswith('\ufeff'))
        self.assertIn('意大利旅游', body)
        self.assertIn('进行中', body)
        self.assertIn('旅行', body)
        self.assertIn('六天五夜', body)

    def test_expense_export_contains_amount_and_note(self):
        resp = self.client.get(reverse('activities:expense_export'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('120.5', body)
        self.assertIn('机票', body)
        self.assertIn('意大利旅游', body)

    def test_export_respects_visibility(self):
        """只导 visible_qs 范围内的数据，不混他人数据"""
        Activity.objects.create(user=self.other, name='别人的秘密活动')
        Expense.objects.create(
            activity=Activity.objects.create(user=self.other, name='别人的日常'),
            user=self.other, note='别人的账', amount=1)
        body = self.client.get(
            reverse('activities:activity_export')).content.decode('utf-8')
        self.assertNotIn('别人的秘密活动', body)
        body = self.client.get(
            reverse('activities:expense_export')).content.decode('utf-8')
        self.assertNotIn('别人的账', body)

    def test_export_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse('activities:activity_export'))
        self.assertEqual(resp.status_code, 302)


class DependencyGraphJsTest(SimpleTestCase):
    """⑨ 依赖图升级：SVG 必须用 DOM API 构建，禁止 innerHTML 拼用户数据

    活动名是用户输入，旧版用 innerHTML 拼 SVG 字符串会把名字里的 <、"
    直接注进 DOM；这个锁剔注释后扫禁用词，防止回潮。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = (Path(settings.BASE_DIR) / 'static' / 'js'
                  / 'dependency-graph.js').read_text(encoding='utf-8')

    @staticmethod
    def _strip_js_comments(src):
        # 注释里写的「禁止 innerHTML」自己包含这个词，不剔就是假失败（本项目三次坑）
        no_block = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        return re.sub(r'^\s*//.*$', '', no_block, flags=re.M)

    def test_no_innerhtml_in_svg_construction(self):
        code = self._strip_js_comments(self.js)
        self.assertNotIn('innerHTML', code)

    def test_uses_dom_api_and_accessibility_attrs(self):
        code = self._strip_js_comments(self.js)
        self.assertIn('createElementNS', code)
        self.assertIn('textContent', code)   # 活动名走 textContent
        self.assertIn('aria-label', code)    # 节点可访问名
        self.assertIn("createElementNS(NS, 'title')", code)  # SVG hover 全名
