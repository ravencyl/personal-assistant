import json
import os
import re
import tempfile
from decimal import Decimal
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.test import TestCase, TransactionTestCase, Client, override_settings
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

from activities.models import Activity, ActivityLog, Attachment, Expense, Participant
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


class DailyDesktopLayoutTest(TestCase):
    """Daily 页桌面两列布局与右列常驻卡回归锁

    为什么锁：Daily 页的两列完全靠模板里两个列容器 + CSS 在 768px 下的
    grid/sticky/order 实现，改回单列、把某块从右列挤进左列、给移动端加了 sm:
    结构断点，都不会报错，只在真实屏幕上退化。

    移动端契约与活动详情页不同：Daily 页的右列整块在 DOM 里排在主内容流之前
    （这样移动端阅读顺序与改造前逐块一致），桌面端靠 .page-cols--rail-first 的
    order 换回右侧 ——所以 DOM 里的块顺序就是移动端顺序契约，下面按它断言。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'activities' / 'daily.html'
    CSS = Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css'
    RAIL_END = '</div><!-- /右列 -->'
    MAIN_END = '</div><!-- /左列 -->'
    COLS_END = '</div><!-- /.page-cols -->'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        today = timezone.localdate()
        trip = Activity.objects.create(user=self.user, name='新西兰之旅', status='in_progress',
                                       start_date=today - timedelta(days=1),
                                       end_date=today + timedelta(days=2))
        expense = Expense.objects.create(activity=trip, user=self.user,
                                         amount=Decimal('600'), paid_at=today)
        apply_tags(expense, ['交通'])
        # 近期完成分组
        Activity.objects.create(user=self.user, name='旧项目结项', status='done',
                                start_date=today - timedelta(days=2))
        self.html = self._render_morning()

    def _render_morning(self):
        """按早间（09:00）渲染：show_today_plan 只在 <18 点为真。

        不固定时段的话，「提醒与子任务」整块会在傍晚以后跑测试时直接不渲染，
        顺序锁与右列归属锁都会静默空跑。只替换「取当前时间」这一种调用，
        模板里日期格式化（传 value 的 localtime）仍走原逻辑。
        """
        from unittest import mock
        real = timezone.localtime
        early = real().replace(hour=9, minute=0, second=0, microsecond=0)

        def fake(value=None, current_timezone=None):
            return early if value is None else real(value, current_timezone)

        with mock.patch.object(timezone, 'localtime', fake):
            return self.client.get('/activities/daily/').content.decode()

    def _at(self, text, anchor, desc):
        """在切片里找锚点位；找不到就是「这块被搬出该列」，当断言失败报而不是 ValueError"""
        at = text.find(anchor)
        if at < 0:
            self.fail(f'{desc}：预期内容不在该列里（找不到 {anchor}）')
        return at

    def _cols(self):
        start = self._at(self.html, 'class="page-cols', '两列容器')
        return self.html[start:self._at(self.html, self.COLS_END, '两列收尾')]

    def _rail(self):
        cols = self._cols()
        start = self._at(cols, 'class="page-rail"', '右列容器')
        return cols[start:self._at(cols, self.RAIL_END, '右列')]

    def _main(self):
        cols = self._cols()
        start = self._at(cols, 'class="page-main"', '左列容器')
        return cols[start:self._at(cols, self.MAIN_END, '左列')]

    def test_two_column_grid_and_exactly_two_columns(self):
        self.assertEqual(self.html.count('class="page-cols'), 1, '两列容器应唯一')
        cols = self._cols()
        self.assertEqual(cols.count('class="page-rail"'), 1, 'page-cols 内应恰好一个右列')
        self.assertEqual(cols.count('class="page-main"'), 1, 'page-cols 内应恰好一个左列')
        self.assertIn(self.RAIL_END, cols, '右列未闭合，两列结构已破损')
        self.assertIn(self.MAIN_END, cols, '左列未闭合，两列结构已破损')

    def test_column_containers_have_no_visibility_class(self):
        """列容器不加显隐类是「移动端视觉顺序 = DOM 顺序」的前提"""
        self.assertIn('<div class="page-rail">', self.html,
                      '右列容器加了类，移动端可能少一整列')
        self.assertIn('<div class="page-main">', self.html,
                      '左列容器加了类，移动端可能少一整列')

    def test_right_column_is_dom_first_and_visually_right_on_desktop(self):
        """右列整块在 DOM 里排在左列之前：移动端顺序才能与改造前逐块一致"""
        cols = self._cols()
        self.assertLess(self._at(cols, 'class="page-rail"', '右列'),
                        self._at(cols, 'class="page-main"', '左列'),
                        '右列改成 DOM 在后会让移动端「提醒与子任务/统计」下跳，阅读顺序回退')
        self.assertIn('page-cols--rail-first', self.html,
                      '缺 rail-first 修饰类：右列在 DOM 前就会出现在桌面左侧，左右颠倒')
        css = self.CSS.read_text(encoding='utf-8')
        rail_order = [b for b in _css_rules(css, '.page-rail') if 'order' in b['body']]
        self.assertTrue(rail_order, '没有把右列换回右侧的 order 声明，桌面端左右会颠倒')
        self.assertIn('order: 2', rail_order[0]['body'])

    def test_primary_flow_left_auxiliary_right(self):
        rail, main = self._rail(), self._main()
        for anchor, desc in [('data-section="daily-plan"', '子任务'), ('今日活动', '今日活动计数'),
                             ('本周消费', '本周消费')]:
            self.assertIn(anchor, rail, f'{desc}应在右列（今日概览）')
            self.assertNotIn(anchor, main, f'{desc}不该出现在左列')
        for anchor, desc in [('新建活动', '快捷入口'), ('今日进行中', '活动分组')]:
            self.assertIn(anchor, main, f'{desc}应在左列主内容流')

    def test_mobile_reading_order_matches_dom(self):
        """移动端单列顺序：与改造前的块序列逐块对齐（含只在桌面出现的进度卡占位）。
        2026-09 移动端改造后：移动端快捷入口从 main 提到右列速览卡下方，
        与三数合一速览卡组成「今日速览」组，故「新建活动」先于「本周消费」"""
        anchors = ['data-section="daily-plan"', '今日活动', '新建活动', '本周消费',
                   '今日进行中']
        positions = [self._at(self.html, a, f'移动端顺序锁定位 {a}') for a in anchors]
        self.assertEqual(positions, sorted(positions),
                         '移动端单列顺序变了：速览卡 + 快捷入口（今日速览组）之后才是本周消费与活动分组')

    def test_no_manual_htmx_process_and_json_tags_clean(self):
        """模板不手动 htmx.process（避免双重绑定），JSON 端点标签不带 hx-*"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        self.assertNotIn('htmx.process', src, '手动 htmx.process 会造成双重绑定与旧节点引用残留')
        for tag in re.findall(r'<[^>]*\bdata-status-url\b[^>]*>', src):
            self.assertNotIn('hx-', tag, 'JSON 端点只能由 fetch 消费，元素上不能挂 hx-*')

    def test_secondary_lists_default_collapsed_on_desktop_only(self):
        """三个次要长列表两端默认折叠（2026-09 改：原来仅桌面折叠，移动端首屏
        被非今日内容挤满，390×844 走查后改为两端同默认）；手动展开后仍由 localStorage 记忆"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        self.assertNotIn("matchMedia('(min-width: 768px)')", src,
                         '默认折叠不再分端门控（两端同默认），不应再出现 768px 断点判断')
        self.assertRegex(src, r"var defaultCollapsed = \[[^\]]*'upcoming'[^\]]*\]",
                         '「即将开始」应保留在默认折叠清单里')
        self.assertRegex(src, r'if \(!state && defaultCollapsed',
                         '默认折叠只能作用于未手动折叠过的区块')
        self.assertIn("localStorage.getItem('daily_section_' + sectionId)", src,
                      '折叠状态仍走既有 localStorage 机制，不另造一套')
        self.assertIn("'daily-plan', 'in_progress', "
                      "'upcoming', 'recently_done'", src,
                      '恢复脚本的分区清单被改，可能有区的折叠状态不再恢复')

    def test_no_structural_sm_breakpoint_in_template(self):
        """分端只允许 md:（768px）；sm: 仅可作纯尺寸渐进（p/gap/text/space）"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        hits = re.findall(r'sm:(hidden|block|flex|inline|grid|order|col-span|row-span|sticky|absolute|fixed)\S*', src)
        self.assertEqual(hits, [], f'模板出现 sm: 结构性断点：{hits}')

    def test_columns_css_uses_single_md_breakpoint(self):
        css = self.CSS.read_text(encoding='utf-8')
        for selector in ('.page-cols', '.page-rail'):
            blocks = _css_rules(css, selector)
            self.assertTrue(blocks, f'custom.css 里 {selector} 的声明丢了')
            for block in blocks:
                self.assertEqual(block['media'], '(min-width: 768px)',
                                 f'{selector} 必须只落在 768px 这个唯一结构断点内')
        grid = _css_rules(css, '.page-cols')[0]
        self.assertEqual(grid['selectors'], '.page-cols',
                         '两列声明必须全站只一份且独立不分组（与别的页面分组共享，一处改会连带飘）')
        self.assertIn('320px', grid['body'])
        self.assertIn('minmax(0, 1fr)', grid['body'], '左列须用 minmax(0,1fr) 兜住长内容撑破列')
        self.assertIn('align-items: start', grid['body'],
                      '网格默认 stretch 会把右列拉高，sticky 就没空间钉住')
        rail = _css_rules(css, '.page-rail')[0]
        self.assertIn('position: sticky', rail['body'])
        self.assertIn('max-height', rail['body'], '矮视口下右列需列内滚动，否则底部信息看不到')

    def test_no_template_syntax_leaked_into_rendered_html(self):
        """模板注释语法泄漏锁：Django 的 {# #} 不支持跳行，写多行会整段渲染成正文"""
        for token in ('{%', '{{', '{#'):
            self.assertNotIn(token, self.html, f'渲染结果里出现 {token}，模板语法泄漏')


class DailyCreateModalTest(TestCase):
    """Daily 页「新建活动」弹窗接入回归锁

    为什么锁：入口从独立创建页改为与列表页共用的 Lightbox 弹窗，改回去
    （链接跳 /activities/new/ 或局部拷一份弹窗 DOM）不会报错，只有体验分裂。
    弹窗骨架的唯一实现在 _activity_create_modal.html，两页共同 include。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'activities' / 'daily.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        self.html = self.client.get('/activities/daily/').content.decode()

    def test_create_entries_open_modal_not_page(self):
        """快捷入口与空态按钮都走弹窗，不再跳独立创建页"""
        src = self.TEMPLATE.read_text(encoding='utf-8')
        # 3 处：桌面端快捷操作区 + 移动端快捷入口（双渲染）+ 空态按钮
        self.assertEqual(src.count('data-open-create-modal'), 3,
                         '桌面操作区 + 移动快捷入口 + 空态应各有一个弹窗入口')
        self.assertNotIn("url 'activities:activity_create'", src,
                         'Daily 页不应再直链独立创建页')

    def test_modal_skeleton_shared_with_list_page(self):
        """弹窗骨架来自共享 partial，两页渲染出同一份 DOM"""
        self.assertIn('{% include "activities/_activity_create_modal.html" %}',
                      self.TEMPLATE.read_text(encoding='utf-8'),
                      '弹窗骨架必须 include 共享 partial，禁止局部拷贝')
        list_src = (Path(settings.BASE_DIR) / 'templates' / 'activities'
                    / 'activity_list.html').read_text(encoding='utf-8')
        self.assertIn('{% include "activities/_activity_create_modal.html" %}', list_src)
        for token in ('id="create-activity-modal"', 'id="modal-quick-input"'):
            self.assertEqual(self.html.count(token), 1, f'{token} 应恰好渲染一份')

    def test_modal_context_and_scripts_rendered(self):
        """视图注入弹窗表单与 chips 联想数据，页面加载弹窗所需三个脚本"""
        self.assertIn('id="create-activity-form"', self.html, '弹窗表单未渲染')
        self.assertIn('id="participant-options"', self.html, '参与者联想数据未渲染')
        self.assertIn('id="tag-options"', self.html, '标签联想数据未渲染')
        for script in ('js/pinyin-pro.js', 'js/activity-form.js', 'js/quick-parse.js'):
            self.assertIn(script, self.html, f'弹窗依赖脚本未加载：{script}')


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
