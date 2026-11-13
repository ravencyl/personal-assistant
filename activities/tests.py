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

from activities.models import Activity, ActivityDraft, ActivityDescriptionTask, ActivityLog, Attachment, Expense, Participant, CalendarFeed
from activities.views.daily_views import daily_brief_payload, gather_daily
from core.agent_registry import CandidateToolError, ToolError
from core.layout_asserts import assert_desktop_two_columns, code_only
from activities.services import (InputError, add_expense,
                                 create_activity_from_parsed, record_parsed_cost,
                                 start_due_activities)
from activities.utils import (get_daily_bucket, DAILY_BUCKET_NAME,
                              DAILY_BUCKET_MARKER, daily_bucket_q, is_daily_bucket,
                              exclude_daily_bucket, resolve_participants,
                              fuzzy_resolve_participants)
from unittest.mock import patch, MagicMock
from django.db import IntegrityError, transaction
from core.models import Notification
from activities.services import (OptimizeBusyError, active_description_task,
                                 build_optimization_prompt, clean_optimized_text,
                                 run_description_optimization,
                                 start_description_optimization,
                                 DESC_OPTIMIZE_STALE_SECONDS)


class AddExpenseAutoTargetTest(TestCase):
    """记账工具 target 缺省时的归属链：日期重叠 → note 关键词 → 日常开支桶"""

    def setUp(self):
        from core.agent_registry import get_tool
        self.tool = get_tool('activities.add_expense')
        self.assertIsNotNone(self.tool)
        self.user = User.objects.create_user('testuser', password='test')
        self.today = timezone.localdate()

    def _confirm(self, params):
        """预览 → 确认执行两步；顺带锁「预览阶段绝不落库」"""
        preview = self.tool['fn'](self.user, params)
        self.assertEqual(preview['card'], 'confirm',
                         'add_expense 预览必须出确认卡（2026-09-22 两步流）')
        self.assertFalse(Expense.objects.filter(user=self.user).exists(),
                         '预览阶段就落了库，确认流形同虚设')
        return self.tool['apply'](self.user, params)

    def test_no_target_fallback_to_daily_bucket(self):
        """无 target 且无可归属活动时，费用记入「日常开支」归属桶"""
        result = self._confirm({'amount': 25, 'tags': ['餐饮'], 'note': '午饭'})
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
        result = self._confirm({'amount': 30, 'tags': ['交通'], 'note': '上海 打车 35'})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity.name, '出差上海')
        self.assertIn('出差上海', result['reply'])

    def test_with_target_original_path(self):
        """有 target 时走原匹配路径，行为不变"""
        activity = Activity.objects.create(user=self.user, name='周末露营')
        result = self._confirm({'target': '露营', 'amount': 120, 'tags': ['购物']})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity_id, activity.id)
        # 标签随费用落库，回复里带上标签后缀
        self.assertEqual(tag_names(expense), ['购物'])
        self.assertEqual(result['reply'], '已为「周末露营」添加费用 ¥120.00（购物）')

    def test_no_target_date_overlap_unique(self):
        """无 target 时当日/昨日日期重叠的唯一进行中活动优先命中"""
        Activity.objects.create(
            user=self.user, name='桐庐旅行', status='in_progress',
            start_date=self.today - timedelta(days=1),
            end_date=self.today + timedelta(days=1),
        )
        result = self._confirm({'amount': 66, 'tags': ['餐饮']})
        expense = Expense.objects.get(user=self.user)
        self.assertEqual(expense.activity.name, '桐庐旅行')
        self.assertIn('桐庐旅行', result['reply'])

    def test_bucket_hidden_from_activity_list_page(self):
        """归属桶不出现在活动列表页，但费用统计口径包含桶内费用"""
        self._confirm({'amount': 10})
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
    """活动详情页内联手动创建活动安排（subactivity_manual_create 端点）"""

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
        """超管创建活动安排，归属仍是父活动 owner（子活动继承父活动 user）"""
        User.objects.create_superuser('root', password='root')
        admin_client = Client()
        admin_client.login(username='root', password='root')
        resp = admin_client.post(
            f'/activities/{self.parent.id}/subactivities/manual-create/',
            data=json.dumps({'name': '租用车'}), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Activity.objects.get(name='租用车').user_id, self.user.id)

    def test_logs_written_for_parent_and_child(self):
        """父子两条活动日志，与 add_subactivity 口径一致"""
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
        """详情页渲染内联表单（默认折叠）"""
        Activity.objects.create(user=self.user, name='已有活动安排', parent=self.parent)
        resp = self.client.get(f'/activities/{self.parent.id}/')
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('手动添加活动安排', content)
        self.assertIn('id="sub-manual-form" class="hidden', content)
        self.assertIn('novalidate', content)  # 行内错误接管浏览器原生必填气泡
        self.assertNotIn('快速记一笔活动安排', content)  # AI 快速入口已随快记下线删除
        self.assertIn('已有活动安排', content)
        self.assertIn('sub-manual-tag-options', content)  # 标签 autocomplete 建议

    def test_subtask_timeline_sorts_by_start_date_descending(self):
        """时间轴按可用日期倒序（2026-09-22 用户要求：最近的排最上面），无日期仍排最后"""
        Activity.objects.create(user=self.user, parent=self.parent, name='旧',
                                start_date=date(2026, 1, 5))
        Activity.objects.create(user=self.user, parent=self.parent, name='新',
                                start_date=date(2026, 9, 20))
        Activity.objects.create(user=self.user, parent=self.parent, name='没日期')
        resp = self.client.get(f'/activities/{self.parent.id}/')
        content = resp.content.decode()
        # 只看活动安排时间轴区块：「未设定」等字样在页面其它卡也会出现
        block = content[content.index('id="subtask-list"'):]
        self.assertLess(block.index('>新<'), block.index('>旧<'),
                        '时间轴应从晚到早：新任务在旧任务前面')
        self.assertLess(block.index('>旧<'), block.index('未设定'),
                        '无日期的仍排最后')


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


class ResolveParticipantsFuzzyTest(TestCase):
    """模糊识别（只读）：口语名 → 已有联系人，三级证据，分 matched/ambiguous/missing。

    用户诉求：联系人里是「KeYing Li」，说「keying」要能认出来；确认不存在后
    才可与用户确认新建。本套件锁识别口径，绝不写库（新建在工具层确认后才发生）。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.keying = Participant.objects.create(user=self.user, name='KeYing Li')
        self.joey = Participant.objects.create(user=self.user, name='Joey')
        self.joeyan = Participant.objects.create(user=self.user, name='Joe Yan')
        self.likeying = Participant.objects.create(user=self.user, name='李可盈')

    def test_token_match_keying_to_keying_li(self):
        r = fuzzy_resolve_participants(self.user, ['keying'])
        self.assertEqual(r['matched'], [('keying', self.keying, 'token')])
        self.assertEqual(r['ambiguous'], [])
        self.assertEqual(r['missing'], [])

    def test_exact_full_name_preferred(self):
        r = fuzzy_resolve_participants(self.user, ['KeYing  Li'])
        self.assertEqual(r['matched'], [('KeYing  Li', self.keying, 'exact')])

    def test_exact_beats_token_when_both_exist(self):
        bare_li = Participant.objects.create(user=self.user, name='Li')
        r = fuzzy_resolve_participants(self.user, ['li'])
        # 「li」精确等于联系人「Li」，尽管它也是「KeYing Li」的整词——精确级优先
        self.assertEqual(r['matched'], [('li', bare_li, 'exact')])
        self.assertEqual(r['ambiguous'], [])

    def test_ambiguous_when_token_hits_multiple(self):
        alice = Participant.objects.create(user=self.user, name='Alice Li')
        r = fuzzy_resolve_participants(self.user, ['li'])
        self.assertEqual(r['matched'], [])
        self.assertEqual(r['missing'], [])
        self.assertEqual(len(r['ambiguous']), 1)
        spoken, candidates = r['ambiguous'][0]
        self.assertEqual(spoken, 'li')
        self.assertEqual({p.name for p in candidates}, {'KeYing Li', 'Alice Li'})

    def test_joey_and_joe_yan_stay_distinct(self):
        # AGENTS.md 明令 Joey ≠ Joe Yan：整词/子串是结构证据，不是字符重合度
        r = fuzzy_resolve_participants(self.user, ['joey'])
        self.assertEqual(r['matched'], [('joey', self.joey, 'exact')])
        r2 = fuzzy_resolve_participants(self.user, ['joe yan'])
        self.assertEqual(r2['matched'], [('joe yan', self.joeyan, 'exact')])

    def test_token_beats_substring(self):
        # 「joe」是「Joe Yan」的整词（token），又恰是「Joey」的前缀（substring）；
        # 取最强一级 token → 唯一命中 Joe Yan，不被弱证据稀释成歧义
        r = fuzzy_resolve_participants(self.user, ['joe'])
        self.assertEqual(r['matched'], [('joe', self.joeyan, 'token')])
        self.assertEqual(r['ambiguous'], [])

    def test_chinese_substring_match(self):
        r = fuzzy_resolve_participants(self.user, ['可盈'])
        self.assertEqual(r['matched'], [('可盈', self.likeying, 'substring')])

    def test_single_char_not_substring_matched(self):
        # 单字「李」长度 <2，不参与子串匹配（太易误伤），落 missing
        r = fuzzy_resolve_participants(self.user, ['李'])
        self.assertEqual(r['matched'], [])
        self.assertEqual(r['missing'], ['李'])

    def test_case_whitespace_and_at_prefix(self):
        r = fuzzy_resolve_participants(self.user, ['  @KEYING  '])
        self.assertEqual(r['matched'], [('KEYING', self.keying, 'token')])

    def test_missing_when_no_evidence(self):
        r = fuzzy_resolve_participants(self.user, ['路人甲'])
        self.assertEqual(r['matched'], [])
        self.assertEqual(r['ambiguous'], [])
        self.assertEqual(r['missing'], ['路人甲'])

    def test_mixed_categories_in_one_call(self):
        alice = Participant.objects.create(user=self.user, name='Alice Li')
        r = fuzzy_resolve_participants(self.user, ['keying', 'li', '路人甲'])
        self.assertEqual(r['matched'], [('keying', self.keying, 'token')])
        self.assertEqual({p.name for p in r['ambiguous'][0][1]}, {'KeYing Li', 'Alice Li'})
        self.assertEqual(r['missing'], ['路人甲'])

    def test_blank_input(self):
        empty = {'matched': [], 'ambiguous': [], 'missing': []}
        self.assertEqual(fuzzy_resolve_participants(self.user, []), empty)
        self.assertEqual(fuzzy_resolve_participants(self.user, None), empty)
        self.assertEqual(fuzzy_resolve_participants(self.user, ['  ', '']), empty)

    def test_other_users_contacts_are_invisible(self):
        r = fuzzy_resolve_participants(self.other, ['keying'])
        self.assertEqual(r['matched'], [])
        self.assertEqual(r['missing'], ['keying'])

    def test_read_only_never_writes(self):
        before = Participant.objects.count()
        fuzzy_resolve_participants(self.user, ['keying', 'li', '路人甲', '可盈'])
        self.assertEqual(Participant.objects.count(), before)

    def test_resolve_participants_still_exact_only(self):
        # 模糊识别不得改变唯一写入口的精确口径：「keying」精确匹配仍落空
        matched, skipped, created = resolve_participants(self.user, ['keying'])
        self.assertEqual(matched, [])
        self.assertEqual(skipped, ['keying'])
        self.assertEqual(created, [])


class ParticipantAgentToolTest(TestCase):
    """AI 对话创建/修改活动：只填已有参与者，不自动新建联系人"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.yyx = Participant.objects.create(user=self.user, name='YYX')

    def test_create_tool_skips_unknown_participant(self):
        from core.agent_registry import get_tool
        tool = get_tool('activities.create')
        params = {'name': '和 yyx 吃饭', 'participants': ['yyx', '路人甲']}
        # 两步确认流：预览不落库，确认后才创建
        preview = tool['fn'](self.user, params)
        self.assertEqual(preview['card'], 'confirm')
        self.assertFalse(Activity.objects.filter(name='和 yyx 吃饭').exists())
        self.assertIn('路人甲', preview['reply'])
        self.assertIn('未添加', preview['reply'])
        result = tool['apply'](self.user, params)
        activity = Activity.objects.get(id=result['activity_ids'][0])
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['YYX'])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists())

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


class ActivityCreateConfirmFlowTest(TestCase):
    """activities.create 两步确认流（2026-09-22，用户要求：创建必须出确认卡）

    起因：网络卡顿时模型连发几十条 create，同名空活动被静默批量落库。
    之后 create 一律先出预览确认卡，用户点确认才真正创建（与 update/delete 同一
    套 confirm_action 端点，apply 复用 _create_plan 单一口径）。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        self.tool = get_tool('activities.create')
        self.user = User.objects.create_user('testuser', password='test')

    def test_preview_creates_nothing_and_returns_confirm_card(self):
        result = self.tool['fn'](self.user, {
            'name': '去杭州团建', 'start_date': '2026-10-01', 'cost': 3000})
        self.assertEqual(result['card'], 'confirm')
        self.assertEqual(result['card_data']['kind'], 'create')
        self.assertEqual(result['action']['tool'], 'activities.create')
        self.assertEqual(result['action']['params']['name'], '去杭州团建')
        self.assertFalse(Activity.objects.exists(), '预览阶段不得落库')
        fields = {f['label']: f['value'] for f in result['card_data']['fields']}
        self.assertEqual(fields['日期'], '2026-10-01')
        self.assertEqual(fields['费用'], '¥3000')
        self.assertIn('请确认', result['reply'])

    def test_apply_after_confirm_creates_with_same_fields(self):
        params = {'name': '去杭州团建', 'start_date': '2026-10-01', 'cost': 3000}
        self.tool['fn'](self.user, params)
        result = self.tool['apply'](self.user, params)
        activity = Activity.objects.get(id=result['activity_ids'][0])
        self.assertEqual(activity.name, '去杭州团建')
        self.assertEqual(activity.start_date.isoformat(), '2026-10-01')
        self.assertEqual(activity.expenses.count(), 1)
        self.assertTrue(result['created'])
        self.assertIn('已创建', result['reply'])

    def test_cancel_path_writes_nothing(self):
        """确认卡上点取消只是标记 resolved，不调 apply 就没有任何写入"""
        self.tool['fn'](self.user, {'name': '不想建的活动'})
        self.assertFalse(Activity.objects.exists())

    def test_parent_not_found_is_surfaced_in_preview(self):
        result = self.tool['fn'](self.user, {'name': '买机票', 'parent': '不存在的父活动'})
        self.assertIn('没找到', result['reply'])
        self.assertFalse(Activity.objects.filter(name='不存在的父活动').exists())

    def test_preview_without_name_raises_tool_error(self):
        with self.assertRaises(ToolError):
            self.tool['fn'](self.user, {'start_date': '2026-10-01'})


class ActivityCreateDraftFlowTest(TestCase):
    """activities.create 跨轮持久草稿（2026-10-28，用户要求：信息不全时多轮对话引导补充）

    旧口径：name-only 一句话直接出确认卡，确认卡上只有一个「名称」字段——
    点确认就落一条没日期没费用的空活动。新口径：没有日期时先落草稿（一个对话
    一份），回复里说明已记下什么、还缺什么；用户补齐后才出确认卡。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        from chat.models import Conversation
        self.tool = get_tool('activities.create')
        self.user = User.objects.create_user('testuser', password='test')
        self.conversation = Conversation.objects.create(
            user=self.user, session_id='sess-draft-test')
        self.cid = self.conversation.id

    def _draft(self):
        from activities.models import ActivityDraft
        return ActivityDraft.objects.get(conversation_id=self.cid)

    def test_missing_date_saves_draft_and_guides(self):
        result = self.tool['fn'](self.user, {
            'name': '去杭州团建', 'cost': 3000, '_conversation_id': self.cid})
        self.assertNotIn('card', result, '信息不全时不出确认卡')
        self.assertIn('草稿', result['reply'])
        self.assertIn('还缺日期', result['reply'])
        self.assertIn('3000', result['reply'], '已确认的字段要复述给用户')
        self.assertIn('不需要日期', result['reply'], '要给 force 出口')
        draft = self._draft()
        self.assertEqual(draft.name, '去杭州团建')
        self.assertEqual(draft.params['cost'], 3000)
        self.assertFalse(Activity.objects.exists(), '草稿不是活动')

    def test_second_turn_merges_draft_and_emits_confirm_card(self):
        self.tool['fn'](self.user, {
            'name': '去杭州团建', 'cost': 3000, '_conversation_id': self.cid})
        result = self.tool['fn'](self.user, {
            'name': '去杭州团建', 'start_date': '2026-11-05',
            'start_time': '14:00', '_conversation_id': self.cid})
        self.assertEqual(result['card'], 'confirm')
        fields = {f['label']: f['value'] for f in result['card_data']['fields']}
        self.assertEqual(fields['日期'], '2026-11-05 14:00')
        self.assertEqual(fields['费用'], '¥3000', '第一轮的费用要从草稿带回来')
        # action 自带合并参数：确认重放不依赖草稿表（草稿此刻已删）
        self.assertEqual(result['action']['params']['cost'], 3000)
        self.assertEqual(result['action']['params']['start_date'], '2026-11-05')
        self.assertFalse(
            ActivityDraft.objects.filter(conversation_id=self.cid).exists(),
            '信息已齐草稿即删')

    def test_apply_after_confirm_replays_merged_params(self):
        self.tool['fn'](self.user, {
            'name': '去杭州团建', 'cost': 3000, '_conversation_id': self.cid})
        self.tool['fn'](self.user, {
            'name': '去杭州团建', 'start_date': '2026-11-05', '_conversation_id': self.cid})
        result = self.tool['apply'](self.user, {
            'name': '去杭州团建', 'cost': 3000, 'start_date': '2026-11-05'})
        activity = Activity.objects.get(id=result['activity_ids'][0])
        self.assertEqual(activity.start_date.isoformat(), '2026-11-05')
        self.assertEqual(activity.expenses.count(), 1)

    def test_new_name_replaces_draft(self):
        """用户换了个新活动：旧草稿的字段（尤其费用）不能串到新草稿上"""
        self.tool['fn'](self.user, {
            'name': '活动甲', 'cost': 100, '_conversation_id': self.cid})
        self.tool['fn'](self.user, {
            'name': '活动乙', '_conversation_id': self.cid})
        draft = self._draft()
        self.assertEqual(draft.name, '活动乙')
        self.assertNotIn('cost', draft.params, '旧活动的费用不得串台')
        self.assertEqual(ActivityDraft.objects.filter(user=self.user).count(), 1,
                         '一个对话一份')

    def test_force_skips_draft_and_writes_no_draft(self):
        result = self.tool['fn'](self.user, {
            'name': '不确定日期的事', 'force': True, '_conversation_id': self.cid})
        self.assertEqual(result['card'], 'confirm')
        self.assertFalse(ActivityDraft.objects.exists())

    def test_expired_draft_is_cleared_and_treated_as_absent(self):
        self.tool['fn'](self.user, {
            'name': '旧草稿', 'cost': 1, '_conversation_id': self.cid})
        # updated_at 是 auto_now：时间回填必须走 queryset.update（T3 首跑教训）
        ActivityDraft.objects.filter(conversation_id=self.cid).update(
            updated_at=timezone.now() - timedelta(days=4))
        result = self.tool['fn'](self.user, {
            'name': '旧草稿', 'cost': 2, '_conversation_id': self.cid})
        self.assertNotIn('card', result, '过期草稿不参与合并，仍按信息不全走草稿')
        self.assertEqual(self._draft().params['cost'], 2, '过期草稿已清，不残留旧值')

    def test_no_conversation_id_keeps_confirm_card(self):
        """旁路调用（无对话归位点）保持旧确认卡行为，绝不落孤儿草稿"""
        result = self.tool['fn'](self.user, {'name': '名字而已'})
        self.assertEqual(result['card'], 'confirm')
        self.assertFalse(ActivityDraft.objects.exists())

    def test_orchestrator_injects_conversation_id_into_params(self):
        """编排器把 conversation_id 透给工具（跨轮草稿的归位前提）——直接对
        _dispatch 走一条完整协议 JSON，不依赖模型真发"""
        from core.agent_registry import ChatOrchestrator
        # intent 用短名（INTENT_TOOL_MAP 的键），与模型真实输出同形
        protocol = json.dumps({
            'intent': 'create',
            'params': {'name': '编排器直达', 'cost': 50},
            'reply': '我来帮你创建',
        }, ensure_ascii=False)
        content, payload, changed = ChatOrchestrator()._dispatch(
            self.user, protocol, conversation_id=self.cid)
        self.assertIn('草稿', content)
        self.assertEqual(self._draft().params['cost'], 50)
        self.assertFalse(changed)

    def test_draft_reminder_for_active_draft_only(self):
        from activities.agent_tools import draft_reminder
        self.assertEqual(draft_reminder(self.conversation), '', '无草稿时是空串')
        self.tool['fn'](self.user, {
            'name': '去杭州团建', 'cost': 3000, '_conversation_id': self.cid})
        hint = draft_reminder(self.conversation)
        self.assertIn('去杭州团建', hint)
        self.assertIn('还缺日期', hint)
        self.assertIn('activities.create', hint)


class ParticipantFuzzyCreateFlowTest(TestCase):
    """创建活动接入模糊识别 + 确认新建（用户诉求：keying→KeYing Li；确认不存在后才代建联系人）

    口径：唯一命中（含模糊）直接用规范名；歧义/缺失在对话路径落草稿引导多轮澄清；
    新建联系人是写操作，必须 create_participants=True（用户确认后模型才传）；
    无 conversation_id 的旁路保持旧确认卡行为（缺失只跳过不新建）。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        from chat.models import Conversation
        self.tool = get_tool('activities.create')
        self.user = User.objects.create_user('testuser', password='test')
        self.keying = Participant.objects.create(user=self.user, name='KeYing Li')
        self.conversation = Conversation.objects.create(
            user=self.user, session_id='sess-fuzzy-test')
        self.cid = self.conversation.id

    def _draft(self):
        return ActivityDraft.objects.get(conversation_id=self.cid)

    def _fields(self, result):
        return {f['label']: f['value'] for f in result['card_data']['fields']}

    def test_fuzzy_unique_match_uses_canonical_name(self):
        params = {'name': '和 keying 吃晚饭', 'start_date': '2026-11-20',
                  'participants': ['keying'], '_conversation_id': self.cid}
        preview = self.tool['fn'](self.user, params)
        self.assertEqual(preview['card'], 'confirm')
        self.assertEqual(self._fields(preview)['参与者'], 'KeYing Li')
        self.assertIn('识别为', preview['reply'])
        self.assertIn('KeYing Li', preview['reply'])
        applied = self.tool['apply'](self.user, preview['action']['params'])
        activity = Activity.objects.get(id=applied['activity_ids'][0])
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['KeYing Li'])
        self.assertEqual(Participant.objects.filter(user=self.user).count(), 1, '模糊命中不新建')

    def test_exact_match_not_announced_as_fuzzy(self):
        preview = self.tool['fn'](self.user, {
            'name': '晚饭', 'start_date': '2026-11-20',
            'participants': ['KeYing Li'], '_conversation_id': self.cid})
        self.assertEqual(preview['card'], 'confirm')
        self.assertNotIn('识别为', preview['reply'], '精确命中无需复述模糊映射')

    def test_missing_participant_holds_for_confirmation(self):
        preview = self.tool['fn'](self.user, {
            'name': '和路人甲吃饭', 'start_date': '2026-11-20',
            'participants': ['路人甲'], '_conversation_id': self.cid})
        self.assertNotIn('card', preview, '缺失参与者不出确认卡，先澄清')
        self.assertIn('草稿', preview['reply'])
        self.assertIn('新建', preview['reply'])
        self.assertIn('路人甲', preview['reply'])
        self.assertEqual(self._draft().params['participants'], ['路人甲'])
        self.assertFalse(Activity.objects.exists())
        self.assertFalse(Participant.objects.filter(name='路人甲').exists())

    def test_create_participants_flag_creates_contact_after_confirm(self):
        params = {'name': '和路人甲吃饭', 'start_date': '2026-11-20',
                  'participants': ['路人甲'], 'create_participants': True,
                  '_conversation_id': self.cid}
        preview = self.tool['fn'](self.user, params)
        self.assertEqual(preview['card'], 'confirm')
        self.assertIn('路人甲（新建联系人）', self._fields(preview)['参与者'])
        self.assertIn('将新建联系人', preview['reply'])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists(), '预览阶段不建联系人')
        applied = self.tool['apply'](self.user, preview['action']['params'])
        activity = Activity.objects.get(id=applied['activity_ids'][0])
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['路人甲'])
        self.assertTrue(Participant.objects.filter(name='路人甲').exists())
        self.assertIn('已新建联系人', applied['reply'])

    def test_ambiguous_participant_holds_for_clarification(self):
        Participant.objects.create(user=self.user, name='Alice Li')
        preview = self.tool['fn'](self.user, {
            'name': '和 li 吃饭', 'start_date': '2026-11-20',
            'participants': ['li'], '_conversation_id': self.cid})
        self.assertNotIn('card', preview)
        self.assertIn('匹配到多个', preview['reply'])
        self.assertIn('KeYing Li', preview['reply'])
        self.assertIn('Alice Li', preview['reply'])
        self.assertFalse(Activity.objects.exists())

    def test_multi_round_missing_then_consent_merges_draft(self):
        # 第一轮：有日期但参与者缺失 → 落草稿澄清（不是缺日期）
        r1 = self.tool['fn'](self.user, {
            'name': '和路人甲吃饭', 'start_date': '2026-11-20',
            'participants': ['路人甲'], '_conversation_id': self.cid})
        self.assertNotIn('card', r1)
        self.assertNotIn('还缺日期', r1['reply'], '日期已给，缺口只在参与者')
        # 第二轮：用户确认新建 → 模型带 create_participants，草稿合并出确认卡
        r2 = self.tool['fn'](self.user, {
            'name': '和路人甲吃饭', 'create_participants': True,
            '_conversation_id': self.cid})
        self.assertEqual(r2['card'], 'confirm')
        self.assertEqual(self._fields(r2)['日期'], '2026-11-20', '日期从草稿带回')
        self.assertIn('路人甲（新建联系人）', self._fields(r2)['参与者'])
        self.assertFalse(ActivityDraft.objects.filter(conversation_id=self.cid).exists())
        applied = self.tool['apply'](self.user, r2['action']['params'])
        activity = Activity.objects.get(id=applied['activity_ids'][0])
        self.assertEqual(activity.start_date.isoformat(), '2026-11-20')
        self.assertTrue(Participant.objects.filter(name='路人甲').exists())

    def test_no_cid_bypass_keeps_confirm_card_and_skips_missing(self):
        preview = self.tool['fn'](self.user, {
            'name': '晚饭', 'start_date': '2026-11-20',
            'participants': ['keying', '路人甲']})
        self.assertEqual(preview['card'], 'confirm', '无对话归位点保持旧确认卡行为')
        self.assertIn('路人甲', preview['reply'])
        self.assertIn('未添加', preview['reply'])
        applied = self.tool['apply'](self.user, preview['action']['params'])
        activity = Activity.objects.get(id=applied['activity_ids'][0])
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['KeYing Li'])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists())

    def test_force_bypasses_hold_but_never_creates_without_consent(self):
        preview = self.tool['fn'](self.user, {
            'name': '和路人甲吃饭', 'start_date': '2026-11-20',
            'participants': ['路人甲'], 'force': True, '_conversation_id': self.cid})
        self.assertEqual(preview['card'], 'confirm', 'force 豁免澄清门')
        self.assertIn('未添加', preview['reply'])
        self.assertFalse(ActivityDraft.objects.exists())
        applied = self.tool['apply'](self.user, preview['action']['params'])
        activity = Activity.objects.get(id=applied['activity_ids'][0])
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), [])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists(),
                         'force 不等于新建联系人的同意')

    def test_draft_reminder_mentions_participant_gap(self):
        from activities.agent_tools import draft_reminder
        self.tool['fn'](self.user, {
            'name': '和路人甲吃饭', 'start_date': '2026-11-20',
            'participants': ['路人甲'], '_conversation_id': self.cid})
        hint = draft_reminder(self.conversation)
        self.assertIn('和路人甲吃饭', hint)
        self.assertIn('参与者', hint)
        self.assertIn('create_participants', hint)


class ParticipantFuzzyUpdateFlowTest(TestCase):
    """修改活动接入模糊识别 + 确认新建（与 create 同口径，但 update 无草稿机制）

    口径：唯一命中（含模糊）用规范名替换；歧义/缺失保留原参与者不变（「未找到」≠「清空」）
    并在回复里追问/给新建出口；新建联系人是写操作，必须 create_participants=True 才落库。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        self.tool = get_tool('activities.update')
        self.user = User.objects.create_user('testuser', password='test')
        self.keying = Participant.objects.create(user=self.user, name='KeYing Li')
        self.li = Participant.objects.create(user=self.user, name='小李')

    def test_fuzzy_match_replaces_with_canonical_name(self):
        activity = Activity.objects.create(user=self.user, name='周末游')
        activity.participants.set([self.li])
        preview = self.tool['fn'](self.user, {'target': '周末游', 'participants': ['keying']})
        self.assertEqual(preview['card'], 'confirm')
        self.assertIn('识别为', preview['reply'])
        self.assertIn('KeYing Li', preview['reply'])
        applied = self.tool['apply'](self.user, preview['action']['params'])
        activity.refresh_from_db()
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['KeYing Li'])
        self.assertEqual(Participant.objects.filter(user=self.user).count(), 2, '模糊命中不新建')

    def test_missing_keeps_original_and_offers_create(self):
        activity = Activity.objects.create(user=self.user, name='周末游')
        activity.participants.set([self.keying])
        preview = self.tool['fn'](self.user, {'target': '周末游', 'participants': ['路人甲']})
        self.assertIn('未添加', preview['reply'])
        self.assertIn('新建', preview['reply'])
        applied = self.tool['apply'](self.user, {'target_id': activity.id, 'participants': ['路人甲']})
        activity.refresh_from_db()
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['KeYing Li'],
                         '全部未命中保留原参与者，不清空')
        self.assertFalse(Participant.objects.filter(name='路人甲').exists())
        self.assertIn('未添加', applied['reply'])

    def test_consent_creates_contact_after_confirm(self):
        activity = Activity.objects.create(user=self.user, name='周末游')
        preview = self.tool['fn'](self.user, {
            'target': '周末游', 'participants': ['路人甲'], 'create_participants': True})
        self.assertEqual(preview['card'], 'confirm')
        self.assertIn('将新建联系人', preview['reply'])
        self.assertFalse(Participant.objects.filter(name='路人甲').exists(), '预览阶段不建联系人')
        applied = self.tool['apply'](self.user, preview['action']['params'])
        activity.refresh_from_db()
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['路人甲'])
        self.assertTrue(Participant.objects.filter(name='路人甲').exists())
        self.assertIn('已新建联系人', applied['reply'])

    def test_ambiguous_keeps_original_and_asks(self):
        Participant.objects.create(user=self.user, name='Alice Li')
        activity = Activity.objects.create(user=self.user, name='周末游')
        activity.participants.set([self.keying])
        preview = self.tool['fn'](self.user, {'target': '周末游', 'participants': ['li']})
        self.assertIn('匹配到多个', preview['reply'])
        self.assertIn('KeYing Li', preview['reply'])
        self.assertIn('Alice Li', preview['reply'])
        applied = self.tool['apply'](self.user, {'target_id': activity.id, 'participants': ['li']})
        activity.refresh_from_db()
        self.assertEqual(list(activity.participants.values_list('name', flat=True)), ['KeYing Li'],
                         '歧义未澄清前保留原参与者')


class ExpenseConfirmFlowTest(TestCase):
    """activities.add_expense 两步确认流（2026-09-22，用户要求：记费用也先确认）

    起因：用户发现「添加一个费用 测试 30元」直接落库，与 create 的确认流
    口径不一致。之后 add_expense 与 create/update/delete 同一套 confirm
    流：预览卡展示归属（含归属理由）与金额，apply 复用 _expense_plan
    单一口径确定性重放。
    """

    def setUp(self):
        from core.agent_registry import get_tool
        self.tool = get_tool('activities.add_expense')
        self.user = User.objects.create_user('testuser', password='test')

    def test_preview_records_nothing_and_returns_confirm_card(self):
        activity = Activity.objects.create(user=self.user, name='约一个')
        result = self.tool['fn'](self.user, {
            'target': '约一个', 'amount': 30, 'note': '测试',
            'paid_at': '2026-09-22', 'tags': ['餐饮']})
        self.assertEqual(result['card'], 'confirm')
        self.assertEqual(result['card_data']['kind'], 'expense')
        self.assertEqual(result['action']['tool'], 'activities.add_expense')
        self.assertEqual(result['action']['params']['amount'], 30)
        self.assertFalse(Expense.objects.exists(), '预览阶段不得落库')
        fields = {f['label']: f['value'] for f in result['card_data']['fields']}
        self.assertIn('约一个', fields['归属'])
        self.assertIn('你指定的活动', fields['归属'], '预览必须写明归属理由')
        self.assertEqual(fields['金额'], '¥30')
        self.assertEqual(fields['日期'], '2026-09-22')
        self.assertEqual(fields['备注'], '测试')
        self.assertEqual(fields['标签'], '餐饮')
        self.assertIn('请确认', result['reply'])
        self.assertIsNotNone(activity)

    def test_apply_after_confirm_records_expense(self):
        activity = Activity.objects.create(user=self.user, name='约一个')
        params = {'target': '约一个', 'amount': 30, 'note': '测试',
                  'paid_at': '2026-09-22', 'tags': '餐饮'}
        self.tool['fn'](self.user, params)
        result = self.tool['apply'](self.user, params)
        expense = Expense.objects.get()
        self.assertEqual(expense.activity_id, activity.id)
        self.assertEqual(expense.amount, Decimal('30'))
        self.assertEqual(expense.paid_at.isoformat(), '2026-09-22')
        self.assertEqual(tag_names(expense), ['餐饮'])
        self.assertTrue(result['changed'])
        # 语义锁：加费用不是创建活动，不宣称 created（预防将来有人误接回填逻辑）
        self.assertNotIn('created', result)

    def test_missing_amount_raises_in_preview_and_records_nothing(self):
        with self.assertRaises(ToolError):
            self.tool['fn'](self.user, {'amount': ''})
        self.assertEqual(Expense.objects.count(), 0)

    def test_auto_target_preview_and_apply_replay_same_bucket(self):
        """无 target 时预览与 apply 确定性重放同一归属（单一口径）"""
        self.tool['fn'](self.user, {'amount': 25, 'note': '午饭'})
        preview_bucket = get_daily_bucket(self.user)
        self.assertEqual(Expense.objects.count(), 0)
        self.tool['apply'](self.user, {'amount': 25, 'note': '午饭'})
        self.assertEqual(Expense.objects.get().activity_id, preview_bucket.id)


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
        preview = self.tool['fn'](self.user, {'target': str(activity.id), 'amount': 66})
        self.assertEqual(preview['card'], 'confirm')
        self.assertEqual(Expense.objects.count(), 0, '预览阶段不得落库')
        self.tool['apply'](self.user, {'target': str(activity.id), 'amount': 66})
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
        self.tool['apply'](self.user, {'target': '项目2026', 'amount': 10})
        self.assertEqual(Expense.objects.get().activity.name, '项目2026')


class ActivityIdDisplayTest(SimpleTestCase):
    """活动 ID 展示锁：只在对话卡片标 #N（列表页已按 2026-10-12 用户决定隐藏——
    一行里多一个数字只会歪歪扭扭，要看 ID 去详情页，口述操作在对话卡片上点）"""

    EXPECTED = {
        'templates/chat/cards/activity_card.html': ['#{{ card.id }}', '#{{ c.id }}'],
        'templates/chat/cards/activity_list_card.html': ['#{{ item.id }}'],
        'templates/chat/cards/candidates_card.html': ['#{{ item.id }}'],
        'templates/chat/cards/daily_brief_card.html': ['#{{ item.id }}'],
        'templates/chat/cards/today_brief_card.html': ['#{{ item.id }}'],
    }

    def test_all_surfaces_render_activity_id(self):
        root = Path(__file__).resolve().parent.parent
        for rel, markers in self.EXPECTED.items():
            html = (root / rel).read_text(encoding='utf-8')
            for m in markers:
                self.assertIn(m, html, f'{rel} 缺少活动 ID 标注 {m}')

    def test_activity_list_no_longer_shows_id(self):
        """防复活：列表页（移动卡 + 桌面表格）都不得再把 #ID 摆回名字旁边"""
        src = (Path(__file__).resolve().parent.parent
               / 'templates' / 'activities' / 'activity_list.html').read_text()
        self.assertNotIn('#{{ activity.id }}', src)


class ManualSubactivityParticipantTest(TestCase):
    """内联手动创建活动安排：未命中的参与者跳过并在响应 note 中说明
    （quick-create / quick-sub 两个一句话端点已于 2026-10-28 随快记功能整体下线删除）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.yyx = Participant.objects.create(user=self.user, name='YYX')
        self.parent = Activity.objects.create(user=self.user, name='新西兰之旅')

    def test_quick_endpoints_gone(self):
        """墓碑：一句话创建的两个端点不得复活（404）"""
        self.assertEqual(self.client.post('/activities/quick-create/',
                                          data='{}', content_type='application/json').status_code, 404)
        self.assertEqual(self.client.post(f'/activities/{self.parent.id}/quick-sub/',
                                          data='{}', content_type='application/json').status_code, 404)

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

    def test_brief_payload_excludes_recently_done_section(self):
        """简报卡不展示「近期完成」（2026-10-08 用户要求：已完成的事不需要再提醒）。
        gather_daily 仍算 recently_done（行为洞察要用），只在 payload 分区里剔除"""
        Activity.objects.create(user=self.user, name='今日已打卡',
                                start_date=self.today, status='done')
        payload = daily_brief_payload(self.user)
        self.assertNotIn(
            'recently_done', [s['key'] for s in payload['sections']],
            '简报卡分区不应再包含近期完成')
        self.assertNotIn('近期完成', [s['title'] for s in payload['sections']])


class DailyPageRetiredTest(TestCase):
    """/daily/ 页面下线（2026-09-19）：路由保留但重定向回首页（= 对话列表），
    旧书签 / 推送 / 模板 url 标签不破；渲染层已删除，任何残留在 template 里都会
    TemplateDoesNotExist。模板文件墓碑收进 core/tests.py::TombstoneRegistryTest。"""

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


class QuickParseRemovedTest(TestCase):
    """快记（一句话解析）前端通道 2026-10-28 整体下线：墓碑防复活。
    quick-parse.js、parsing.py、parse-quick-input / quick-create / quick-sub
    三个端点已全链删除，任何页面都不得再加载/接线解析通道。
    （脚本/解析器/SW 清单的文件级墓碑收进 core/tests.py::TombstoneRegistryTest，
    这里只锁「渲染出来的页面」——静态文件墓碑拦不住模板里新写的接线。）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')
        self.activity = Activity.objects.create(user=self.user, name='新西兰之旅')

    def _page_ok_and_clean(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn('js/quick-parse.js', html)
        self.assertNotIn('PaQuickParse', html)
        self.assertNotIn('sub-quick-', html)
        self.assertNotIn('form-quick-input', html)
        return html

    def test_detail_page_has_no_quick_parse(self):
        self._page_ok_and_clean(
            reverse('activities:activity_detail', args=[self.activity.id]))

    def test_create_page_has_no_quick_parse(self):
        self._page_ok_and_clean(reverse('activities:activity_create'))

    def test_list_page_has_no_quick_parse(self):
        self._page_ok_and_clean(reverse('activities:activity_list'))

    def test_parse_endpoint_gone(self):
        self.assertEqual(
            self.client.post('/activities/parse-quick-input/',
                             data='text=x', content_type='application/x-www-form-urlencoded').status_code,
            404)


class FilterPanelUsabilityTest(TestCase):
    """筛选面板可用性：激活摘要 chips（折叠时可读、可单项移除）+ 状态常驻 + 标签块下线

    之前折叠条只有「N 项生效」，展开后 24+ 个标签 chip 铺三四行。
    折叠条必须能一眼看出生效的是哪几项、每项能单独 × 掉。
    2026-10-08 用户定策：状态筛选（全部/计划/进行中/已完成）拉出折叠区常驻；
    折叠区里的标签筛选块整个下线（使用率太低），折叠区只留日期筛选——
    标签只能点活动卡上的 #词 进入筛选。
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

    def test_status_chips_outside_collapsible_body(self):
        """状态 chips 必须常驻在 filter-body 之外：折叠状态下（无筛选参数）也要可见"""
        html = self._html()
        body = html.split('id="filter-body"', 1)
        self.assertEqual(len(body), 2)
        self.assertIn('>全部</a>', body[0], '状态「全部」应在折叠区之外常驻')
        for label in ('计划', '进行中', '已完成'):
            self.assertIn(label, body[0], f'状态筛选 {label} 应常驻可见')

    def test_tag_filter_block_removed_from_panel(self):
        """折叠区不再有标签筛选块（含「更多 N 个标签」折叠件）；URL tag 仍生效且摘要可移除"""
        self._seed_overflow_tags()
        html = re.sub(r'<!--.*?-->', '', self._html(), flags=re.S)  # 剔模板注释，避免静态文案误命中
        self.assertNotIn('标签筛选', html)
        self.assertNotIn('id="tag-more"', html)
        self.assertNotIn('tag-more-toggle', html)
        # URL 带 tag 仍能筛选，且折叠条摘要展示并可单独移除
        html2 = self._html(tag='标签01', status='planned')
        self.assertIn('# 标签01', html2)
        qs_list = self._qs_dicts(self._remove_hrefs(html2))
        self.assertIn({'status': 'planned'}, qs_list, '移除标签后应保留其他筛选')


class ActivityCreateModalFormTest(TestCase):
    """新建活动弹窗表单体（2026-10-08 真机反馈两处）

    - 块间距：字段 partial 的顶层 div 是 #create-modal-body 的直接子元素，
      form.space-y-4 隔着包裹层传不进来，包裹层必须自带 space-y-4（否则块与块紧贴）；
    - chips 联想数据：列表页视图漏传 all_tags 时弹窗里标签候选永远是空的，
      「可从下方建议中选择」的提示成了空话——标签/参与者候选必须都在。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def _html(self):
        resp = self.client.get(reverse('activities:activity_list'))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_modal_body_carries_its_own_spacing(self):
        html = self._html()
        self.assertRegex(html, r'<div id="create-modal-body" class="[^"]*space-y-4')
        # 字段块的父层有间距类还不够，得确认 form.space-y-4 与包裹层同时存在
        self.assertIn('class="space-y-4"', html)

    def test_modal_has_tag_and_participant_options(self):
        act = Activity.objects.create(user=self.user, name='候选容器')
        apply_tags(act, ['差旅'])
        Participant.objects.create(user=self.user, name='YYX')
        html = self._html()
        m = re.search(r'<script id="tag-options" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        self.assertIn('差旅', json.loads(m.group(1)), '弹窗标签候选应含已有标签')
        m2 = re.search(r'<script id="participant-options" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m2)
        self.assertIn('YYX', json.loads(m2.group(1)), '弹窗参与者候选应含已有联系人')


class ActivityCreateWizardTest(TestCase):
    """建活动分步向导（2026-10-16）：创建页与新建弹窗共用五步制，编辑页保持单屏。

    步序为用户定稿（标签 → 名称与描述 → 时间与状态 → 参与者 → 层级），改序即改产品口径；
    单页切步（同一 <form>），服务端 POST/校验不变，所以锁只看结构标记不看字段本身。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def test_create_page_renders_five_steps(self):
        html = self.client.get(reverse('activities:activity_create')).content.decode()
        self.assertEqual(re.findall(r'data-step="(\d+)"', html), ['1', '2', '3', '4', '5'],
                         '创建页五步结构缺失或步序变了')
        self.assertIn('js/activity-wizard.js', html, '向导脚本未加载')
        # 提交按钮必须保留防重复提交脚本绑定的 id
        self.assertIn('id="activity-save-btn"', html)
        # 标签墙（步①）与标签候选数据都要在
        self.assertIn('id="tag-wall"', html)
        self.assertIn('id="tag-options"', html)
        # 层级三选一：独立 / 父活动 / 子任务
        self.assertEqual(re.findall(r'name="hierarchy" value="(\w+)"', html),
                         ['standalone', 'parent', 'children'])

    def test_edit_page_stays_flat(self):
        """编辑态不分步：字段已填过，走五步反而多余"""
        activity = Activity.objects.create(user=self.user, name='编辑态')
        html = self.client.get(
            reverse('activities:activity_edit', args=[activity.id])).content.decode()
        self.assertNotIn('data-wizard', html)
        self.assertIn('id="activity-save-btn"', html)

    def test_modal_carries_wizard(self):
        html = self.client.get(reverse('activities:activity_list')).content.decode()
        self.assertEqual(re.findall(r'data-step="(\d+)"', html), ['1', '2', '3', '4', '5'],
                         '新建弹窗五步结构缺失')
        self.assertIn('js/activity-wizard.js', html, '列表页未加载向导脚本')
        self.assertIn('id="create-modal-submit"', html, '弹窗提交按钮 id 被 fetch 流程依赖，不能丢')
        self.assertIn('data-wizard-cancel', html, '弹窗取消按钮（overlay 委托绑定）不能丢')
        # 弹窗口径：不出现父活动选项（hide_parent）
        self.assertNotIn('name="hierarchy" value="parent"', html)

    def test_invalid_submit_surfaces_non_field_error(self):
        """422 换体必须带得出错的标记与文案（2026-10-21 线上循环实锤）

        跨字段校验（结束日期<开始日期）走 non-field 通道；partial 若不渲染它的
        js-form-error 段落，换体后向导找不到落点、用户零提示地重走五步再被拒。
        """
        resp = self.client.post(reverse('activities:activity_create'), {
            'name': '日期倒挂',
            'start_date': '2026-10-25',
            'end_date': '2026-10-20',
        }, HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 422)
        html = resp.content.decode()
        self.assertIn('js-form-error', html, '422 体里没有任何错误标记，向导无法落位')
        self.assertIn('结束日期不能早于开始日期', html, 'non-field 错误文案未渲染，用户会零反馈循环')

    def test_wizard_js_lands_on_buried_error(self):
        """向导 JS 的错误落位必须处理子步与折叠容器（静态锁，剔注释后断言）"""
        src = (Path(settings.BASE_DIR) / 'static' / 'js' / 'activity-wizard.js').read_text()
        code = re.sub(r'/\*[\s\S]*?\*/|//[^\n]*', '', src)
        self.assertIn("data-time-substep-panel", code,
                      '错误落位未处理步③子步：错误在 3.2/3.3 时用户停在 3.1 看不见')
        self.assertIn('#end-date-row', code,
                      '错误落位未摊开收起的结束日期行：end_date 错误渲染在默认 hidden 的行里')

    def test_wizard_js_syncs_end_date_from_start(self):
        """开始日期晚于结束日期时 JS 自动拉平结束日期（静态锁，剔注释后断言）

        结束日期行默认隐藏，但隐藏行里的 initial「今天」会随表单提交——
        开始日期选到未来时若不同步，提交必撞 non-field 422（结束日期不能早于开始日期）。
        """
        src = (Path(settings.BASE_DIR) / 'static' / 'js' / 'activity-wizard.js').read_text()
        code = re.sub(r'/\*[\s\S]*?\*/|//[^\n]*', '', src)
        self.assertIn("getElementById('id_start_date')", code, '向导 JS 未绑定开始日期输入')
        self.assertIn("getElementById('id_end_date')", code, '向导 JS 未引用结束日期输入')
        self.assertIn('endDateInput.value < start', code,
                      '缺少「结束日期早于开始日期才同步」的比较，会覆盖用户已选的跨天结束日期')

    def test_create_form_defaults_both_dates_to_today(self):
        """新建时开始/结束日期都预填今天（结束行虽默认隐藏，值也要在）"""
        today = timezone.localdate().isoformat()
        for url_name in ('activities:activity_create',):
            resp = self.client.get(reverse(url_name))
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode()
            for field_id in ('id_start_date', 'id_end_date'):
                m = re.search(r'<input[^>]*id="%s"[^>]*>' % field_id, html)
                self.assertIsNotNone(m, f'{url_name} 页面未渲染 {field_id}')
                v = re.search(r'value="([^"]*)"', m.group(0))
                self.assertIsNotNone(v, f'{field_id} 渲染了但没有 value（默认今天丢失）')
                self.assertEqual(v.group(1), today, f'{field_id} 默认值不是今天')


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


class CostVsBudgetEndpointsTest(TestCase):
    """费用记支出，新建表单入口口径一致
    （快速创建端点已于 2026-10-28 随快记功能整体下线删除）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

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


class TimeParsingTest(TestCase):
    """normalize_input 的时间归一：孤时间丢弃、越界丢弃、补零
    （规则解析器 activities/parsing.py 已于 2026-10-28 随快记功能整体下线删除）"""

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

    def test_time_band_words(self):
        """时段词兜底（2026-11-10）：模型把「晚上」原样写进 params 时不再静默丢弃

        start 侧取区间起点、end 侧取区间终点——「周三晚上吃饭」只有时段词时
        也能落出一段时间，而不是只剩一个日期。
        """
        from activities.utils import normalize_input
        out = normalize_input({'name': '晚饭', 'start_date': '2026-11-11',
                               'start_time': '晚上', 'end_time': '晚上'}, date(2026, 11, 10))
        self.assertEqual(out['start_time'], '18:00')
        self.assertEqual(out['end_time'], '22:00')

    def test_time_band_with_clock(self):
        """时段词+钟点：「下午3点」=15:00、「晚上7点半」=19:30、裸「7点」=07:00"""
        from activities.utils import normalize_input
        base = {'name': 'x', 'start_date': '2026-11-11'}
        self.assertEqual(normalize_input({**base, 'start_time': '下午3点'},
                                         date(2026, 11, 10))['start_time'], '15:00')
        self.assertEqual(normalize_input({**base, 'start_time': '晚上7点半'},
                                         date(2026, 11, 10))['start_time'], '19:30')
        self.assertEqual(normalize_input({**base, 'start_time': '7点'},
                                         date(2026, 11, 10))['start_time'], '07:00')
        self.assertEqual(normalize_input({**base, 'end_time': '晚上8点'},
                                         date(2026, 11, 10))['end_time'], '20:00')
        self.assertEqual(normalize_input({**base, 'start_time': '下午三点'},
                                         date(2026, 11, 10))['start_time'], '15:00')
        self.assertEqual(normalize_input({**base, 'start_time': '晚上6点30分'},
                                         date(2026, 11, 10))['start_time'], '18:30')

    def test_time_token_garbage_still_dropped(self):
        """反向锁：解析不出的杂文本照旧丢弃，不产生脏时间"""
        from activities.utils import normalize_input
        base = {'name': 'x', 'start_date': '2026-11-11'}
        for junk in ('随时', '看情况', '25点', '晚上 18:00 左右', '6点70分'):
            with self.subTest(value=junk):
                out = normalize_input({**base, 'start_time': junk}, date(2026, 11, 10))
                self.assertNotIn('start_time', out)


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

    def test_add_subactivity_endpoint_keeps_both_logs(self):
        """快捷建活动安排改走 services 后，仍要写父+子两条日志且只建一个对象"""
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

    def test_badge_renders_from_status_class(self):
        """状态信息由徽章（图标+文字）承载；顶部色条已随 2026-10-27 整站去色删除"""
        activity = Activity.objects.create(user=self.user, name='桐庐周末游',
                                           status='in_progress')
        html = self.client.get(
            reverse('activities:activity_detail', args=[activity.id])).content.decode()
        self.assertIn('badge-status--in_progress', html)
        self.assertNotIn('top_bar_color', html)

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
                             ('id="subtask-list"', '活动安排时间轴')]:
            self.assertIn(anchor, left, f'{desc}应在左列主内容流')
            self.assertNotIn(anchor, right, f'{desc}不该出现在右列')
        for anchor, desc in [('id="attachment-form"', '附件'), ('参与者（', '参与者'),
                             ('相关知识', '关联内容')]:
            self.assertIn(anchor, right, f'{desc}应在右列辅助信息')

    def test_mobile_reading_order_matches_dom(self):
        """移动端列容器不带显隐类，视觉顺序 = DOM 顺序；这里锁住约定的顺序"""
        anchors = ['活动描述', 'id="expense-form"', 'id="subtask-list"',
                   'id="attachment-form"', '参与者（', '相关知识', '操作历史（']
        positions = [self.html.index(a) for a in anchors]
        self.assertEqual(positions, sorted(positions),
                         '移动端单列顺序变了（活动安排应紧跟费用明细，右列整体在其后）')
        cols = self._columns()
        for c in re.findall(r'<div class="[^"]*">', cols)[:2]:
            self.assertNotIn('hidden', c, '列容器加了显隐类会让移动端少一整列')

    def test_left_column_long_cards_are_details_collapsed_by_default(self):
        """左列两张长卡（费用明细/活动安排）用 details 默认折叠（2026-09-21 用户要求）

        锁两层：① 必须是 details 而不是普通 div（可点击展开）；② 摘要标签后不得紧跟
        open 属性（默认收起，页面才不会拉得太长）。已有 data-mobile-collapse 的
        右列卡不受影响（那些是桌面展开、移动端 JS 收起的口径）。
        """
        for title in ('费用明细', '活动安排'):
            i = self.html.index(f'>{title}（')
            summary_start = self.html.rindex('<summary', 0, i)
            details_start = self.html.rindex('<details', 0, summary_start)
            opening = self.html[details_start:summary_start]
            self.assertIn('<details', opening, f'{title}卡必须是 details（可折叠）')
            self.assertNotIn(' open', opening.split('>')[0],
                             f'{title}卡应默认折叠，open 会让页面又变长')
            summary = self.html[summary_start:self.html.index('</summary>', i)]
            self.assertIn('justify-between', summary, f'{title}计数与箭头要分居两端')

    def test_quick_action_card_is_desktop_only_and_inside_right_column(self):
        cols = self._columns()
        right = cols.split(self.LEFT_END, 1)[1]
        card = right[:right.index('附件区域')]
        self.assertIn('hidden md:block', card,
                      '快捷操作卡必须只在桌面端出现，否则移动端白占高度')
        self.assertEqual(card.count('data-jump='), 3, '记一笔/加活动安排/传附件三个入口缺一')

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
        """概览增强：活动安排完成度进度条"""
        self.assertIn('title="活动安排完成度 1/2"', self.html)
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
        """符合条件的活动安排也进日历，描述里带父活动名"""
        parent = self._mk(name='父活动')
        child = Activity.objects.create(user=self.user, name='活动安排', parent=parent,
                                        status='planned', start_date=self.today,
                                        end_date=self.today)
        body = self.client.get(self._feed_url()).content.decode()
        self.assertIn('活动安排', body)
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
        child = Activity.objects.create(user=self.user, name='边界活动安排',
                                        parent=tops[-1])
        r1 = self.client.get('/activities/')
        self.assertEqual(self._top_level_count(r1), 20)
        self.assertNotIn('边界活动安排', r1.content.decode())  # 子活动留在父活动所在页

        r2 = self.client.get('/activities/?page=2')
        self.assertEqual(self._top_level_count(r2), 5)  # 子活动不计入
        c2 = r2.content.decode()
        self.assertIn('活动25', c2)
        self.assertIn('边界活动安排', c2)  # 与父活动同页

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


class AdminExportActionTest(TestCase):
    """admin 批量导出（2026-10-08 前台导出入口下线后，admin 是唯一导出入口）

    列口径必须与前台 CSV 下载同一份（activity_csv_rows / expense_csv_rows），
    带同样 BOM；只导 action 勾选的行，不是全量。
    """

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            'admin', 'admin@example.com', password='test')
        self.user = User.objects.create_user('raven', password='test')
        self.activity = Activity.objects.create(user=self.user, name='意大利旅游')
        self.other_activity = Activity.objects.create(user=self.user, name='不勾选的活动')
        self.expense = Expense.objects.create(
            activity=self.activity, user=self.user, amount=Decimal('120.50'),
            paid_at=timezone.localdate(), note='机票')
        self.client.force_login(self.superuser)

    def test_activity_admin_action_exports_only_selected(self):
        resp = self.client.post(reverse('admin:activities_activity_changelist'), {
            'action': 'export_selected_csv',
            '_selected_action': [str(self.activity.id)],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp['Content-Type'])
        body = resp.content.decode('utf-8')
        self.assertTrue(body.startswith('\ufeff'))
        self.assertIn('意大利旅游', body)
        self.assertNotIn('不勾选的活动', body)

    def test_expense_admin_action_exports_selected_rows(self):
        resp = self.client.post(reverse('admin:activities_expense_changelist'), {
            'action': 'export_selected_csv',
            '_selected_action': [str(self.expense.id)],
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertTrue(body.startswith('\ufeff'))
        self.assertIn('120.5', body)
        self.assertIn('机票', body)


class AiTagGuardTest(TestCase):
    """AI 标签守门与「AI 自动加标签」下线（2026-09-21 守门 / 2026-10-08 下线）

    之前模型自由编造标签直接落库（apply_tags 对新名字 get_or_create），
    加上 suggest_tags 兑底无脑塞高频常用标签，导致「AI 乱贴标签」。
    第一阶段修复：AI 推断只匹配已有标签，编造的丢弃并告知。
    第二阶段（用户仍不满意）：自动加标签整体下线——用户没显式要求标签时
    什么都不贴，suggest_tags 函数连同三个调用入口一并拆除。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')

    def _seed(self):
        """预建「出差」「餐饮」标签 + 一个带「出差」标签的历史活动"""
        from core.models import Tag
        Tag.objects.get_or_create(scope='activity', name='出差', defaults={'is_active': True})
        Tag.objects.get_or_create(scope='activity', name='餐饮', defaults={'is_active': True})
        base = Activity.objects.create(user=self.user, name='上海出差三天')
        apply_tags(base, ['出差'])
        return base

    def test_resolve_existing_tags_drops_invented(self):
        from core.models import Tag
        from core.tags import resolve_existing_tags
        self._seed()
        matched, skipped = resolve_existing_tags(['出差', '编造标签'], 'activity', self.user)
        self.assertEqual([t.name for t in matched], ['出差'])
        self.assertEqual(skipped, ['编造标签'])
        self.assertFalse(Tag.objects.filter(name='编造标签').exists(),
                         'AI 编造的标签不得新建 Tag 行')

    def test_create_tool_drops_invented_tags(self):
        from core.agent_registry import get_tool
        self._seed()
        tool = get_tool('activities.create')
        params = {'name': '去上海出差', 'tags': ['出差', '编造标签']}
        preview = tool['fn'](self.user, params)
        self.assertEqual(preview['card'], 'confirm')
        self.assertIn('编造标签', preview['reply'])
        self.assertIn('未添加', preview['reply'])
        result = tool['apply'](self.user, params)
        activity = Activity.objects.get(id=result['activity_ids'][0])
        self.assertEqual(list(activity.tags.values_list('name', flat=True)), ['出差'])
        self.assertFalse(Tag.objects.filter(name='编造标签').exists(),
                         'AI 编造的标签不得新建 Tag 行')

    def test_create_fallback_suggests_only_relevant_tags(self):
        """无用户指定时：内容无关的高频常用标签不再被无脑塞上"""
        from core.agent_registry import get_tool
        self._seed()
        # 历史上「出差」用得最多，但新活动内容与之无关
        for i in range(3):
            act = Activity.objects.create(user=self.user, name=f'出差事项{i}')
            apply_tags(act, ['出差'])
        tool = get_tool('activities.create')
        result = tool['apply'](self.user, {'name': '超市买菜'})
        activity = Activity.objects.get(id=result['activity_ids'][0])
        self.assertEqual(list(activity.tags.values_list('name', flat=True)), [],
                         '无关活动不应被贴上高频常用标签')

    def test_create_fallback_suggests_content_relevant_tag(self):
        """自动加标签已整体下线：即使名称高度相似、标签库里就有对应标签，
        用户没显式要求也一个标签都不贴（2026-10-08）"""
        from core.agent_registry import get_tool
        self._seed()
        result = get_tool('activities.create')['apply'](self.user, {'name': '去 上海出差'})
        activity = Activity.objects.get(id=result['activity_ids'][0])
        self.assertEqual(list(activity.tags.values_list('name', flat=True)), [])

    def test_update_preview_and_apply_filter_invented_tags(self):
        from core.agent_registry import get_tool
        from core.models import Tag
        self._seed()
        activity = Activity.objects.create(user=self.user, name='周末游')
        apply_tags(activity, ['出差'])
        tool = get_tool('activities.update')

        preview = tool['fn'](self.user, {'target': '周末游', 'tags': ['出差', '编造标签']})
        self.assertIn('编造标签', preview['reply'])
        self.assertIn('未添加', preview['reply'])

        tool['apply'](self.user, {'target_id': activity.id, 'tags': ['出差', '编造标签']})
        activity.refresh_from_db()
        self.assertEqual(list(activity.tags.values_list('name', flat=True)), ['出差'])
        self.assertFalse(Tag.objects.filter(name='编造标签').exists())

    def test_update_all_invented_keeps_original_tags(self):
        """全部未命中时保持原标签不变（「未找到」不等于「清空」），不产生变更项"""
        from core.agent_registry import get_tool
        self._seed()
        activity = Activity.objects.create(user=self.user, name='周末游')
        apply_tags(activity, ['出差'])
        tool = get_tool('activities.update')
        preview = tool['fn'](self.user, {'target': '周末游', 'tags': ['编造标签']})
        # 全部未命中 → 无变更项、不出确认卡（标签保持原样）
        self.assertEqual(preview['card'], 'activity')
        self.assertIn('没有识别到', preview['reply'])
        tool['apply'](self.user, {'target_id': activity.id, 'tags': ['编造标签']})
        activity.refresh_from_db()
        self.assertEqual(list(activity.tags.values_list('name', flat=True)), ['出差'])


class ActivityPinTest(TestCase):
    """活动置顶（2026-09-23）：pinned 活动单独渲染在列表上方的「已置顶」区域，
    与主列表分开，无视筛选/分页。

    端点 POST /activities/<id>/pin/ 为 toggle（JSON，原生 fetch 专用）；
    列表视图把置顶组（顶级连同后代树 / 置顶子活动独立提取）放进
    pinned_activities 上下文，主列表剔除避免重复出现。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.client = Client()
        self.client.login(username='testuser', password='test')

    def test_pin_toggle_roundtrip(self):
        a = Activity.objects.create(user=self.user, name='团建')
        resp = self.client.post(reverse('activities:activity_pin_toggle', args=[a.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'id': a.id, 'pinned': True})
        a.refresh_from_db()
        self.assertTrue(a.pinned)
        self.assertIsNotNone(a.pinned_at)
        resp = self.client.post(reverse('activities:activity_pin_toggle', args=[a.id]))
        self.assertEqual(resp.json()['pinned'], False)
        a.refresh_from_db()
        self.assertFalse(a.pinned)
        self.assertIsNone(a.pinned_at)

    def test_pin_endpoint_rejects_get_and_stranger(self):
        """GET 拒绝（toggle 只能 POST）；别人的活动 404（统一无权口径）"""
        a = Activity.objects.create(user=self.user, name='团建')
        self.assertEqual(
            self.client.get(reverse('activities:activity_pin_toggle', args=[a.id])).status_code, 405)
        other = User.objects.create_user('other', password='test')
        stranger_a = Activity.objects.create(user=other, name='别人的')
        self.assertEqual(
            self.client.post(reverse('activities:activity_pin_toggle', args=[stranger_a.id])).status_code, 404)

    def test_pinned_ignores_filters_and_shows_in_section(self):
        """置顶的核心诉求：筛选条件筛不中它也照样出现在上方置顶区"""
        hit = Activity.objects.create(user=self.user, name='团建策划')
        miss = Activity.objects.create(user=self.user, name='理发')
        self.client.post(reverse('activities:activity_pin_toggle', args=[miss.id]))
        resp = self.client.get(reverse('activities:activity_list'), {'keyword': '团建'})
        rows = list(resp.context['activities'])
        self.assertEqual([a.name for a in rows], ['团建策划'])
        pinned = list(resp.context['pinned_activities'])
        self.assertEqual([a.name for a in pinned], ['理发'])
        self.assertIn('已置顶', resp.content.decode())

    def test_pinned_sub_extracted_from_parent_tree(self):
        """置顶的子活动独立进上方区域（depth 归 0），父树里不再出现"""
        parent = Activity.objects.create(user=self.user, name='杭州之行')
        child = Activity.objects.create(user=self.user, name='订酒店', parent=parent)
        self.client.post(reverse('activities:activity_pin_toggle', args=[child.id]))
        resp = self.client.get(reverse('activities:activity_list'))
        rows = list(resp.context['activities'])
        self.assertEqual([a.name for a in rows], ['杭州之行'])
        self.assertFalse(rows[0].has_children)
        pinned = list(resp.context['pinned_activities'])
        self.assertEqual([a.name for a in pinned], ['订酒店'])
        self.assertEqual(pinned[0].depth, 0)

    def test_pinned_order_latest_first(self):
        a = Activity.objects.create(user=self.user, name='甲')
        b = Activity.objects.create(user=self.user, name='乙')
        self.client.post(reverse('activities:activity_pin_toggle', args=[a.id]))
        self.client.post(reverse('activities:activity_pin_toggle', args=[b.id]))
        resp = self.client.get(reverse('activities:activity_list'))
        self.assertEqual([x.name for x in resp.context['pinned_activities']][:2], ['乙', '甲'])

    def test_pinned_appears_only_in_distinct_section(self):
        """置顶活动只渲染在上方区域一次，不得同时残留在主列表里；
        pin 开关只在详情页（2026-10-08 用户要求列表不展示 pin）"""
        a = Activity.objects.create(user=self.user, name='置顶目标')
        self.client.post(reverse('activities:activity_pin_toggle', args=[a.id]))
        resp = self.client.get(reverse('activities:activity_list'))
        html = resp.content.decode()
        self.assertEqual(html.count('置顶目标'), 1)
        self.assertIn('已置顶', html)
        # 列表不再有任何 pin 开关（剔掉正文注释再断，免得下线说明里的字面量误命中）
        html_no_comments = re.sub(r'<!--.*?-->', '', html, flags=re.S)
        self.assertNotIn('pin-toggle', html_no_comments)
        # 开关挪到详情页：已置顶的活动在详情页标题行显示为取消置顶态
        detail = self.client.get(reverse('activities:activity_detail', args=[a.id]))
        self.assertIn('pin-toggle', detail.content.decode())
        self.assertIn('取消置顶', detail.content.decode())
        # 无置顶时不渲染区域头（断言前剔掉 HTML 注释，避免模板静态注释误命中）
        self.client.post(reverse('activities:activity_pin_toggle', args=[a.id]))
        resp2 = self.client.get(reverse('activities:activity_list'))
        html2 = re.sub(r'<!--.*?-->', '', resp2.content.decode(), flags=re.S)
        self.assertNotIn('已置顶', html2)


class ActivityPinAgentToolTest(TestCase):
    """activities.pin：对话里「把 XX 置顶 / 取消置顶」"""

    def setUp(self):
        from core.agent_registry import get_tool
        self.tool = get_tool('activities.pin')
        self.user = User.objects.create_user('testuser', password='test')

    def test_pin_by_target_and_string_boolean(self):
        a = Activity.objects.create(user=self.user, name='团建')
        result = self.tool['fn'](self.user, {'target': '团建', 'pinned': 'true'})
        self.assertIn('已置顶', result['reply'])
        a.refresh_from_db()
        self.assertTrue(a.pinned)
        result = self.tool['fn'](self.user, {'target': '团建', 'pinned': 'false'})
        self.assertIn('已取消', result['reply'])
        a.refresh_from_db()
        self.assertFalse(a.pinned)

    def test_pin_toggle_without_flag_is_noop_when_same(self):
        a = Activity.objects.create(user=self.user, name='团建')
        self.assertIn('已置顶', self.tool['fn'](self.user, {'target': '团建'})['reply'])
        result = self.tool['fn'](self.user, {'target': '团建', 'pinned': True})
        self.assertIn('已经在置顶里', result['reply'])

    def test_pin_requires_target(self):
        from core.agent_registry import ToolError
        with self.assertRaises(ToolError):
            self.tool['fn'](self.user, {})


class TagColorCardTest(TestCase):
    """标签色已整体下线（2026-10-27 用户决定，Typeframe 单色化）：
    活动列表（桌面行/移动卡/置顶卡/标签列 chip）不再携带 tag-wash、
    --tag-color/--tag-rgb、tag-dot 等任何标签着色机械。Tag.color 字段
    本身保留（admin 可改，避免破坏性迁移），只是不再用于渲染。"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('tagcolor', password='test')
        cls.tagged = Activity.objects.create(
            user=cls.user, name='有标签的活动', status='in_progress',
            start_date=date.today())
        Activity.objects.create(
            user=cls.user, name='无标签活动', status='planned',
            start_date=date.today())
        tag = Tag.objects.create(scope='activity', name='旅游', color='#34d399')
        cls.tagged.tags.add(tag)

    def setUp(self):
        self.client = Client()
        self.client.login(username='tagcolor', password='test')

    def _page(self):
        resp = self.client.get('/activities/')
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_no_tag_color_machinery_in_list_html(self):
        html = self._page()
        for marker in ('tag-wash', 'tag-chip', 'tag-dot', '--tag-color', '--tag-rgb'):
            self.assertNotIn(marker, html)
        # 标签名本身仍要正常展示
        self.assertIn('旅游', html)

    def test_css_no_longer_defines_tag_wash(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css').read_text(
            encoding='utf-8')
        for marker in ('.tag-wash', '.tag-chip', '.tag-dot', '--tag-rgb'):
            self.assertNotIn(marker, css)


class MobileActivityCardDisplayTest(TestCase):
    """移动端活动卡（2026-10-11 用户反馈两条）：
    ① 活动名不得截断 —— 换行把名字展示全（break-words，禁 truncate）；
    ② 状态徽章收成 icon-only 色块 —— 文字太长占宽，颜色+icon 已足够区分四态，
       但 title/aria-label 必须保留（否则读屏与长按都不知道这是什么状态）。
    """

    LIST = Path(settings.BASE_DIR) / 'templates' / 'activities' / 'activity_list.html'
    BADGE = Path(settings.BASE_DIR) / 'templates' / 'activities' / '_status_badge.html'

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='mobile-card', password='x')
        cls.activity = Activity.objects.create(
            user=cls.user, name='话剧《尼罗河上的惨案》超长名字验证换行',
            status='in_progress', start_date=date.today())

    def _mobile_block(self, src):
        start = src.index('id="mobile-activity-list"')
        rest = src[start:]
        end = rest.index('<!-- 分页')
        return rest[:end]

    def test_card_name_wraps_and_never_truncates(self):
        from core.layout_asserts import code_only
        block = code_only(self._mobile_block(self.LIST.read_text(encoding='utf-8')))
        self.assertNotIn('truncate', block,
                         '移动端卡的名字又挂上 truncate 了，长名字会被截断')
        self.assertIn('break-words', block)

    def test_mobile_badge_passes_icon_only(self):
        self.assertIn('icon_only=True', self._mobile_block(
            self.LIST.read_text(encoding='utf-8')))

    def test_badge_partial_keeps_label_for_icon_only(self):
        src = self.BADGE.read_text(encoding='utf-8')
        self.assertIn('aria-label="{{ label }}"', src)
        self.assertIn('title="{{ label }}"', src)
        self.assertIn('{% if not icon_only %}{{ label }}{% endif %}', src)
        # 桌面表格/详情页的带字徽章不得被顺手改掉（不传 icon_only 时原样）
        self.assertIn('{{ label }}</span>', src.replace(
            '{% if not icon_only %}{{ label }}{% endif %}</span>',
            '{{ label }}</span>'))

    def test_rendered_mobile_card_shows_full_name(self):
        self.client.login(username='mobile-card', password='x')
        html = self.client.get(reverse('activities:activity_list')).content.decode()
        block = html[html.index('id="mobile-activity-list"'):]
        block = block[:block.index('<!-- 分页')] if '<!-- 分页' in block else block
        self.assertIn('话剧《尼罗河上的惨案》超长名字验证换行', block)
        # icon-only：徽章形状是紧凑 px-1.5，且没有可见的状态文字
        self.assertIn('px-1.5', block)
        self.assertNotIn('进行中</span>', block)
        self.assertIn('aria-label="进行中"', block)


class ListRowClutterRemovedTest(SimpleTestCase):
    """活动列表行内收敛（2026-10-12 用户走查决定，防复活静态锁）：

    ① 行内编辑按钮整体下线（移动卡 + 桌面表格）：使用习惯是点进详情页再编辑；
    ② 移动端卡片去掉 #ID 与 ¥费用，右侧收敛成「折叠槽(44px)｜日期(固定宽)｜状态 icon」
       三条恒定宽度列 —— 这正是此前「歪歪扭扭」的根因（可变宽度尾部元素互相推挤）；
    ③ 桌面表格的费用列保留（列宽天然对齐，且承担 cost 排序入口），不得顺手删除。
    """

    LIST = Path(settings.BASE_DIR) / 'templates' / 'activities' / 'activity_list.html'

    def _raw(self):
        return self.LIST.read_text(encoding='utf-8')

    def _mobile_block(self):
        """移动端卡整块：与 MobileActivityCardDisplayTest 同一锚点口径（<!-- 分页 收尾），
        再剔注释 —— code_only 会把该锚点一起剔掉，所以必须先切块后剔注释"""
        from core.layout_asserts import code_only
        src = self._raw()
        start = src.index('id="mobile-activity-list"')
        rest = src[start:]
        end = rest.index('<!-- 分页')
        return code_only(rest[:end])

    def test_no_inline_edit_link_anywhere(self):
        from core.layout_asserts import code_only
        self.assertNotIn('activities:activity_edit', code_only(self._raw()))

    def test_mobile_card_has_no_cost_or_edit(self):
        block = self._mobile_block()
        self.assertNotIn('¥', block, '移动端卡的费用又摆回来了')
        self.assertNotIn('show_cost', block)

    def test_mobile_card_right_side_uses_fixed_width_slots(self):
        """对齐的唯一实现：折叠钮槽位与日期列恒定宽，无子活动的行也占位"""
        block = self._mobile_block()
        self.assertIn('shrink-0 w-11 flex items-center justify-center', block)
        self.assertIn('shrink-0 w-10 text-right', block)

    def test_desktop_cost_column_survives(self):
        """桌面表格费用列是对齐的，不能跟着移动端一起删
        （Typeframe 起 ¥ 走 .tf-cur 跨字族小写符 span，与数字分离）"""
        from core.layout_asserts import code_only
        table = code_only(self._raw())
        table = table[table.index('id="activity-list"'):table.index('id="mobile-activity-list"')]
        self.assertIn('<span class="tf-cur">¥</span>{{ activity.show_cost', table)
        self.assertIn("sort={% if sort == 'cost' %}-cost{% else %}cost{% endif %}", table)


class TodayHomeTest(TestCase):
    """「今日」首页（2026-10-16 复活，Bento 视觉，设为站点根路径）

    数据层与 daily 简报卡同源（gather_daily），本锁盯两件事：
    根路径渲染 / 时间线只收今日三组。
    （「按标签看」标签云 2026-11-11 用户决定下线，防复活锁见 TombstoneRegistryTest。）
    """

    def setUp(self):
        self.user = User.objects.create_user('today', password='p')
        self.client = Client()
        self.client.login(username='today', password='p')
        self.today = timezone.localdate()

    def _activity(self, name, **kw):
        defaults = dict(user=self.user, name=name, status='planned',
                        start_date=self.today)
        defaults.update(kw)
        return Activity.objects.create(**defaults)

    def test_root_renders_bento_tiles(self):
        self._activity('今日待办', start_time='14:00')
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('今日概览', html)
        self.assertIn('本月支出', html)
        self.assertIn('今天的时间线', html)
        self.assertNotIn('按标签看', html)
        self.assertIn('今日待办', html)

    def test_timeline_only_takes_today_groups(self):
        """未来 7 天活动进「接下来」卡片流，不混进今日时间线"""
        self._activity('今日的事', start_time='09:00')
        self._activity('下周的事', start_date=self.today + timedelta(days=5))
        html = self.client.get('/').content.decode()
        timeline = html[html.index('今天的时间线'):html.index('本月支出')]
        self.assertIn('今日的事', timeline)
        self.assertNotIn('下周的事', timeline)
        rest = html[html.index('接下来'):]
        self.assertIn('下周的事', rest)

    def test_today_tile_shows_all_tags(self):
        """今日时间线一行也展示全部标签，不只首个（2026-11-13 用户要求）"""
        a = self._activity('今日多标签', start_time='09:00')
        apply_tags(a, ['差旅', '美食', '亲子'])
        html = self.client.get('/').content.decode()
        timeline = html[html.index('今天的时间线'):html.index('本月支出')]
        for name in ('差旅', '美食', '亲子'):
            self.assertIn(f'#{name}', timeline)

    def test_login_required(self):
        self.client.logout()
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp['Location'])


class TodayTimelineTest(TestCase):
    """「接下来」7 天时间线（2026-11-12 用户定策：完全还原 demo 变体A）。

    固定配置：按天分组 / 舒展密度 / 全显示（近深远浅·标签·费用·参与者·周末）/
    天默认展开。双锁：
      · 数据层 _upcoming_timeline —— 分组 / 近深远浅档位 / 周末 / 天内排序 /
        排除口径（cancelled·窗口外）/ 计数 / 区间，走 helper 单测精确断言；
      · 渲染 markup —— 默认展开 / 内联详情 / 标记完成按钮 / 品牌粉今天节点 /
        全显示字段，走页面断言（slice 到 #upcoming-timeline 段，避开今日时间线）。
    状态端点 JSON 分支（时间线「标记完成」）单独一组：落库 + 日志 / 越权 JSON 404 /
    无效 400 / 匿名 401 / HTMX 片段优先于 JSON / 整页表单 redirect。
    """

    def setUp(self):
        self.user = User.objects.create_user('tluser', password='p')
        self.client = Client()
        self.client.login(username='tluser', password='p')
        self.today = timezone.localdate()

    def _act(self, name, off=0, **kw):
        defaults = dict(user=self.user, name=name, status='planned',
                        start_date=self.today + timedelta(days=off))
        defaults.update(kw)
        return Activity.objects.create(**defaults)

    def _data(self):
        from activities.views.daily_views import _upcoming_timeline
        return _upcoming_timeline(self.user, self.today)

    def _section(self):
        html = self.client.get('/').content.decode()
        return html[html.index('upcoming-timeline'):]

    # ── 数据层 ──

    def test_depth_tiers_near_to_far(self):
        """近深远浅档位与 demo depthInk 同口径：0/1→0，2/3→1，4/5→2，6/7→3"""
        for off in range(8):
            self._act(f'd{off}', off=off)
        days, count, _ = self._data()
        self.assertEqual({d['off']: d['depth'] for d in days},
                         {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2, 6: 3, 7: 3})
        self.assertEqual(count, 8)

    def test_groups_by_day(self):
        self._act('今天甲', off=0)
        self._act('今天乙', off=0)
        self._act('后天', off=2)
        days, count, _ = self._data()
        self.assertEqual([d['off'] for d in days], [0, 2])
        self.assertEqual([d['count'] for d in days], [2, 1])
        self.assertEqual(count, 3)
        self.assertTrue(days[0]['is_today'])
        self.assertFalse(days[1]['is_today'])
        self.assertEqual(days[0]['rel_label'], '今天')
        self.assertEqual(days[1]['rel_label'], '后天')

    def test_within_day_sorted_by_time_allday_last(self):
        self._act('晚', off=2, start_time='19:00')
        self._act('早', off=2, start_time='08:00')
        self._act('全天', off=2)
        days, _, _ = self._data()
        day = next(d for d in days if d['off'] == 2)
        self.assertEqual([i['name'] for i in day['items']], ['早', '晚', '全天'])
        self.assertTrue(day['items'][2]['is_allday'])
        self.assertEqual(day['items'][2]['time_label'], '全天')

    def test_cancelled_and_out_of_window_excluded(self):
        self._act('正常', off=1)
        self._act('取消', off=1, status='cancelled')
        self._act('太远', off=8)
        self._act('过去', off=-1)
        days, count, _ = self._data()
        self.assertEqual(count, 1)
        self.assertEqual([i['name'] for d in days for i in d['items']], ['正常'])

    def test_weekend_label(self):
        wk_off = next(off for off in range(8)
                      if (self.today + timedelta(days=off)).weekday() >= 5)
        self._act('周末的事', off=wk_off)
        days, _, _ = self._data()
        wk = next(d for d in days if d['off'] == wk_off)
        self.assertTrue(wk['week_label'].endswith(' · 周末'))
        self.assertNotIn('周末', wk['week_base'])

    def test_range_label(self):
        _, _, rng = self._data()
        end = self.today + timedelta(days=7)
        self.assertEqual(rng, f'{self.today.month}/{self.today.day} – {end.month}/{end.day}')

    def test_cost_thousands_separator(self):
        a = self._act('贵', off=1)
        Expense.objects.create(activity=a, user=self.user, amount=Decimal('12000'))
        days, _, _ = self._data()
        item = days[0]['items'][0]
        self.assertEqual(item['cost_str'], '12,000')

    # ── 渲染 markup ──

    def test_days_expanded_by_default(self):
        self._act('a', off=0)
        self._act('b', off=2)
        sec = self._section()
        self.assertEqual(sec.count('aria-expanded="true"'), 2)
        self.assertNotIn('tl-day collapsed', sec)

    def test_today_node_pink_and_rel(self):
        self._act('今天的事', off=0)
        sec = self._section()
        self.assertIn('tl-node--day now', sec)
        self.assertIn('tl-rel now', sec)
        self.assertIn('今天', sec)

    def test_inline_detail_and_mark_complete_button(self):
        a = self._act('看详情', off=1)
        sec = self._section()
        self.assertIn(f'data-detail="{a.id}"', sec)
        self.assertIn(f'data-done="{a.id}"', sec)
        url = reverse('activities:activity_set_status', args=[a.id])
        self.assertIn(f'data-url="{url}"', sec)
        self.assertIn('标记完成', sec)
        self.assertIn('完整详情', sec)

    def test_all_display_fields_rendered(self):
        a = self._act('全显示', off=1, start_time='10:00', description='一段描述')
        apply_tags(a, ['差旅'])
        a.participants.add(Participant.objects.create(user=self.user, name='YYX'))
        Expense.objects.create(activity=a, user=self.user, amount=Decimal('12000'))
        sec = self._section()
        self.assertIn('#差旅', sec)
        self.assertIn('12,000', sec)
        self.assertIn('YYX', sec)
        self.assertIn('一段描述', sec)
        self.assertIn('10:00', sec)

    def test_multiple_tags_all_rendered(self):
        """多标签活动展示全部标签，不再只取首个（2026-11-13 用户要求）"""
        a = self._act('多标签', off=1)
        apply_tags(a, ['差旅', '美食', '亲子'])
        # 数据层：tags 是完整列表；折叠行 meta_parts 含每个 #名称
        days, _, _ = self._data()
        item = days[0]['items'][0]
        self.assertEqual(set(item['tags']), {'差旅', '美食', '亲子'})
        meta_html = ''.join(str(p) for p in item['meta_parts'])
        for name in ('差旅', '美食', '亲子'):
            self.assertIn(f'#{name}', meta_html)
        # 渲染：折叠 meta 与展开详情都出现全部标签，详情每个标签一个 .td-tag
        sec = self._section()
        for name in ('差旅', '美食', '亲子'):
            self.assertIn(f'#{name}', sec)
        self.assertIn('td-tags', sec)
        self.assertEqual(sec.count('class="td-tag"'), 3)

    def test_done_item_rendered_done(self):
        self._act('已完成', off=1, status='done')
        sec = self._section()
        self.assertIn('tl-item done', sec)
        self.assertIn('取消完成', sec)

    def test_empty_state(self):
        sec = self._section()
        self.assertIn('未来 7 天没有新安排', sec)
        self.assertNotIn('tl-dayhead', sec)

    # ── 状态端点 JSON 分支（时间线「标记完成」）──

    def test_json_mark_complete_updates_db_and_logs(self):
        a = self._act('完成它', off=1)
        url = reverse('activities:activity_set_status', args=[a.id])
        resp = self.client.post(url, {'status': 'done'}, HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['done'])
        self.assertEqual(data['status'], 'done')
        a.refresh_from_db()
        self.assertEqual(a.status, 'done')
        self.assertTrue(ActivityLog.objects.filter(
            activity=a, action='status_changed').exists())

    def test_json_unauthorized_returns_json_404(self):
        other = User.objects.create_user('other', password='p')
        a = Activity.objects.create(user=other, name='别人的', status='planned',
                                    start_date=self.today + timedelta(days=1))
        url = reverse('activities:activity_set_status', args=[a.id])
        resp = self.client.post(url, {'status': 'done'}, HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())
        a.refresh_from_db()
        self.assertEqual(a.status, 'planned')

    def test_json_invalid_status_returns_400(self):
        a = self._act('x', off=1)
        url = reverse('activities:activity_set_status', args=[a.id])
        resp = self.client.post(url, {'status': 'bogus'}, HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.json())
        a.refresh_from_db()
        self.assertEqual(a.status, 'planned')

    def test_json_anonymous_returns_401(self):
        a = self._act('x', off=1)
        self.client.logout()
        url = reverse('activities:activity_set_status', args=[a.id])
        resp = self.client.post(url, {'status': 'done'}, HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.json().get('reauth'))

    def test_htmx_fragment_priority_over_json(self):
        """既有片段消费者即使带 Accept json 也仍拿 _daily_card 片段（HTML），不拿 JSON"""
        a = self._act('x', off=1)
        url = reverse('activities:activity_set_status', args=[a.id])
        resp = self.client.post(url, {'status': 'done'},
                                HTTP_ACCEPT='application/json', HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/html', resp['Content-Type'])
        a.refresh_from_db()
        self.assertEqual(a.status, 'done')

    def test_full_page_form_redirects(self):
        a = self._act('x', off=1)
        url = reverse('activities:activity_set_status', args=[a.id])
        resp = self.client.post(url, {'status': 'done'}, HTTP_REFERER='/')
        self.assertEqual(resp.status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.status, 'done')


class TodayDescriptionEditTest(TestCase):
    """今日页时间线「编辑描述」弹窗（2026-11-12 用户要求：卡片加按钮，点开弹窗直接编辑描述）。

    三组锁：
      · 端点行为 —— GET 取全文（卡片预览是 200 字截断，编辑必须拿全文，否则一保存就冲掉
        200 字之后的内容）/ POST 落库+日志 / 允许清空 / 未变更不重复记日志 / 越权 JSON 404 /
        匿名 401 / 非 GET·POST 405；
      · 模板接线 —— 卡片按钮 data-edit-desc + data-url（reverse）/ 共享弹窗 role=dialog +
        textarea name=description + JS 要查的 id 齐全 / 不挂 hx-*；
      · JS·CSS 接线 —— data-edit-desc 委托开弹窗 / GET·POST 走 paJsonFetch / POST 带 X-CSRFToken /
        就地更新 .td-desc + 广播 activities:changed / Escape 挂弹窗自身不挂 document /
        预览截断与服务端同口径 / 用户数据走 textContent / .td-edit 样式与焦点环。
    """

    @classmethod
    def setUpTestData(cls):
        cls.root = Path(settings.BASE_DIR)
        cls.js = (cls.root / 'static' / 'js' / 'today-timeline.js').read_text(encoding='utf-8')
        cls.js_code = cls._strip_js_comments(cls.js)
        cls.modal = (cls.root / 'templates' / 'activities' /
                     '_description_edit_modal.html').read_text(encoding='utf-8')
        cls.css = (cls.root / 'static' / 'css' / 'today-timeline.css').read_text(encoding='utf-8')

    def setUp(self):
        self.user = User.objects.create_user('descuser', password='p')
        self.client = Client()
        self.client.login(username='descuser', password='p')
        self.today = timezone.localdate()

    @staticmethod
    def _strip_js_comments(src):
        # 与 chat/tests.py 同款：剔块注释 + 整行 // 注释，避免说明散文把禁用词锁打成假失败
        no_block = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        return re.sub(r'^\s*//.*$', '', no_block, flags=re.M)

    def _act(self, name='看爸爸', off=0, **kw):
        defaults = dict(user=self.user, name=name, status='planned',
                        start_date=self.today + timedelta(days=off))
        defaults.update(kw)
        return Activity.objects.create(**defaults)

    def _url(self, a):
        return reverse('activities:activity_edit_description', args=[a.id])

    def _today_section(self):
        html = self.client.get('/').content.decode()
        return html[html.index('upcoming-timeline'):]

    # ── 端点行为 ──

    def test_get_returns_full_description_not_truncated(self):
        full = '描' * 300
        a = self._act(description=full)
        resp = self.client.get(self._url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['id'], a.id)
        self.assertEqual(data['name'], a.name)
        self.assertEqual(data['description'], full)      # 全文，不是 200 字预览
        self.assertEqual(len(data['description']), 300)

    def test_get_empty_description_returns_empty_string(self):
        a = self._act(description='')
        resp = self.client.get(self._url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['description'], '')

    def test_post_saves_and_logs(self):
        a = self._act(description='旧备注')
        resp = self.client.post(self._url(a), {'description': '新备注：结论已定'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['description'], '新备注：结论已定')
        a.refresh_from_db()
        self.assertEqual(a.description, '新备注：结论已定')
        self.assertTrue(ActivityLog.objects.filter(activity=a, action='edited').exists())

    def test_post_clears_description(self):
        a = self._act(description='要清掉的备注')
        resp = self.client.post(self._url(a), {'description': ''},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['description'], '')
        a.refresh_from_db()
        self.assertEqual(a.description, '')

    def test_post_unchanged_does_not_log(self):
        a = self._act(description='一样的内容')
        resp = self.client.post(self._url(a), {'description': '一样的内容'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ActivityLog.objects.filter(activity=a, action='edited').exists())

    def test_other_users_activity_get_and_post_json_404(self):
        other = User.objects.create_user('other2', password='p')
        a = Activity.objects.create(user=other, name='别人的', status='planned',
                                    start_date=self.today, description='私密')
        url = self._url(a)
        g = self.client.get(url, HTTP_ACCEPT='application/json')
        self.assertEqual(g.status_code, 404)
        self.assertIn('error', g.json())
        p = self.client.post(url, {'description': '篡改'}, HTTP_ACCEPT='application/json')
        self.assertEqual(p.status_code, 404)
        self.assertIn('error', p.json())
        a.refresh_from_db()
        self.assertEqual(a.description, '私密')          # 越权写绝不落库

    def test_nonexistent_activity_json_404(self):
        url = reverse('activities:activity_edit_description', args=[999999])
        resp = self.client.get(url, HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())

    def test_anonymous_returns_401(self):
        a = self._act()
        self.client.logout()
        resp = self.client.get(self._url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.json().get('reauth'))

    def test_method_not_allowed(self):
        a = self._act()
        resp = self.client.put(self._url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 405)

    # ── 模板接线 ──

    def test_today_card_has_edit_button_with_url(self):
        a = self._act(off=1)
        sec = self._today_section()
        self.assertIn(f'data-edit-desc="{a.id}"', sec)
        self.assertIn(f'data-url="{self._url(a)}"', sec)
        self.assertIn('编辑描述', sec)
        self.assertIn('aria-haspopup="dialog"', sec)

    def test_modal_partial_included_with_dialog_semantics(self):
        self._act(off=1)
        html = self.client.get('/').content.decode()
        self.assertIn('id="desc-edit-modal"', html)
        self.assertIn('id="desc-edit-panel"', html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn('aria-labelledby="desc-edit-title"', html)
        for el_id in ('desc-edit-name', 'desc-edit-form', 'desc-edit-input',
                      'desc-edit-status', 'desc-edit-error', 'desc-edit-submit',
                      'desc-edit-close', 'desc-edit-cancel'):
            self.assertIn(f'id="{el_id}"', html)
        self.assertIn('name="description"', html)

    def test_edit_button_and_modal_do_not_use_htmx(self):
        # 按钮与弹窗都走原生 fetch，不得挂 hx-*（JsonEndpointNeverHtmxTest 也会自动扫到本端点）
        self.assertNotIn('hx-', code_only(self.modal))
        self._act(off=1)
        for line in self._today_section().splitlines():
            if 'data-edit-desc' in line:
                self.assertNotIn('hx-', line)

    # ── JS 接线 ──

    def test_js_delegates_edit_desc_to_open_modal(self):
        self.assertIn("closest('[data-edit-desc]')", self.js_code)
        self.assertIn('openEditModal(editBtn)', self.js_code)

    def test_js_get_and_post_go_through_shared_json_outlet(self):
        get_body = self.js_code[self.js_code.index('function openEditModal'):
                                self.js_code.index('function closeEditModal')]
        post_body = self.js_code[self.js_code.index('function saveEdit'):
                                 self.js_code.index('if (modal) {')]
        self.assertIn('window.paJsonFetch', get_body)
        self.assertIn("method: 'GET'", get_body)
        self.assertIn('window.paJsonFetch', post_body)
        self.assertIn("method: 'POST'", post_body)

    def test_js_post_sends_csrf_and_description(self):
        self.assertIn("body.set('description', value)", self.js_code)
        self.assertIn("'X-CSRFToken': csrfToken()", self.js_code)

    def test_js_updates_card_desc_and_broadcasts(self):
        self.assertIn('function updateCardDesc', self.js_code)
        self.assertIn("document.dispatchEvent(new CustomEvent('activities:changed'))", self.js_code)
        self.assertIn('descEl.remove()', self.js_code)
        self.assertIn('descEl.textContent = preview', self.js_code)
        self.assertIn("detail.insertBefore(descEl, detail.querySelector('.td-actions')", self.js_code)

    def test_js_preview_truncation_matches_server(self):
        # 与服务端 daily_views 同口径：折叠空白 + 200 字截断
        self.assertIn(".replace(/\\s+/g, ' ').trim()", self.js_code)
        self.assertIn('s.length > 200 ? s.slice(0, 200)', self.js_code)

    def test_js_escape_scoped_to_modal_not_document(self):
        # AGENTS.md 禁全局键盘快捷键：Escape 必须挂弹窗自身
        self.assertIn("modal.addEventListener('keydown'", self.js_code)
        self.assertIn("e.key === 'Escape'", self.js_code)
        self.assertNotIn("document.addEventListener('keydown'", self.js_code)

    def test_js_card_desc_uses_textcontent_not_innerhtml(self):
        # 用户数据（描述）一律 textContent；updateCardDesc 函数体内不得拼 HTML 字符串
        body = self.js_code[self.js_code.index('function updateCardDesc'):
                            self.js_code.index('function saveEdit')]
        self.assertIn('textContent', body)
        self.assertNotIn('innerHTML', body)

    def test_js_focuses_panel_not_textarea_on_open(self):
        self.assertIn('editPanel.focus()', self.js_code)

    def test_js_get_failure_keeps_save_disabled(self):
        # 加载失败绝不让用户在空值上保存（editLoaded 守门）
        self.assertIn('if (!editState || !editLoaded) return', self.js_code)
        self.assertIn('editLoaded = true', self.js_code)

    # ── CSS 接线 ──

    def test_css_styles_edit_button_and_focus_ring(self):
        self.assertIn('#upcoming-timeline .td-edit', self.css)
        self.assertIn('#upcoming-timeline .td-edit:hover', self.css)
        self.assertIn('#upcoming-timeline .td-edit:focus-visible', self.css)
        self.assertIn('#upcoming-timeline .td-ic', self.css)


class DescriptionOptimizeTest(TestCase):
    """活动描述「AI优化」后台任务（2026-11-12 用户要求：弹窗加按钮，后台优化文字，
    优化中锁定不可编辑，完成落站内通知）。

    四组锁：
      · 服务层 —— 提示词四项目标 / 清洗代码块与前缀 / 发起建 running 任务起线程 /
        空描述报错 / 重复发起 Busy / 数据库 partial unique 挡竞态 / 僵尸 running 回收；
      · 后台 worker —— 成功写回描述+日志+成功通知 / 清洗 AI 产出 / 失败置 error+失败通知 /
        空结果按失败 / 任务已非 running 放弃写回 / finally 关连接；
      · 端点 —— 发起 200/空 400/忙 409/越权 404/匿名 401/GET 405；状态 running/done/error/idle；
        编辑 GET 带 optimizing+URL、POST 优化中 409 锁定、完成后可编辑；表单页描述 readonly+提交不改描述；
      · 接线 —— 弹窗 AI 按钮+优化中横幅 / JS 三函数走共享 fetch+CSRF+轮询+广播 / CSS spinner。
    """

    @classmethod
    def setUpTestData(cls):
        cls.root = Path(settings.BASE_DIR)
        cls.js = (cls.root / 'static' / 'js' / 'today-timeline.js').read_text(encoding='utf-8')
        cls.js_code = cls._strip_js_comments(cls.js)
        cls.modal = (cls.root / 'templates' / 'activities' /
                     '_description_edit_modal.html').read_text(encoding='utf-8')
        cls.css = (cls.root / 'static' / 'css' / 'today-timeline.css').read_text(encoding='utf-8')

    def setUp(self):
        self.user = User.objects.create_user('optuser', password='p')
        self.client = Client()
        self.client.login(username='optuser', password='p')
        self.today = timezone.localdate()

    @staticmethod
    def _strip_js_comments(src):
        no_block = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        return re.sub(r'^\s*//.*$', '', no_block, flags=re.M)

    def _act(self, name='看爸爸', description='原始的描述文字', **kw):
        defaults = dict(user=self.user, name=name, status='planned',
                        start_date=self.today, description=description)
        defaults.update(kw)
        return Activity.objects.create(**defaults)

    def _task(self, a, status='running', **kw):
        defaults = dict(activity=a, user=self.user, status=status,
                        original=a.description or '')
        defaults.update(kw)
        return ActivityDescriptionTask.objects.create(**defaults)

    def _opt_url(self, a):
        return reverse('activities:activity_description_optimize', args=[a.id])

    def _status_url(self, a):
        return reverse('activities:activity_description_optimize_status', args=[a.id])

    def _edit_url(self, a):
        return reverse('activities:activity_edit_description', args=[a.id])

    # ── 服务层：提示词与清洗 ──

    def test_prompt_covers_four_goals_and_keeps_facts(self):
        p = build_optimization_prompt('明天去开会', '项目评审')
        for goal in ('逻辑', '错别字', '语病', '用词'):
            self.assertIn(goal, p)
        self.assertIn('不新增事实', p)
        self.assertIn('明天去开会', p)
        self.assertIn('项目评审', p)

    def test_clean_strips_code_fence_and_prefix(self):
        self.assertEqual(clean_optimized_text('```\n优化后的描述：你好世界\n```'), '你好世界')
        self.assertEqual(clean_optimized_text('```text\n正文\n```'), '正文')
        self.assertEqual(clean_optimized_text('优化结果：干净正文'), '干净正文')
        self.assertEqual(clean_optimized_text('  普通正文  '), '普通正文')
        self.assertEqual(clean_optimized_text(''), '')
        self.assertEqual(clean_optimized_text(None), '')

    def test_clean_preserves_paragraphs(self):
        self.assertEqual(clean_optimized_text('第一段\n\n第二段'), '第一段\n\n第二段')

    # ── 服务层：发起与锁 ──

    def test_start_creates_running_task_and_spawns_thread(self):
        a = self._act()
        with patch('activities.services._spawn_optimization') as spawn:
            task = start_description_optimization(a, self.user)
        spawn.assert_called_once_with(task.id)
        self.assertEqual(task.status, 'running')
        self.assertEqual(task.original, a.description)
        self.assertEqual(task.activity_id, a.id)
        self.assertEqual(task.user_id, self.user.id)

    def test_start_empty_description_raises_inputerror(self):
        a = self._act(description='   ')
        with patch('activities.services._spawn_optimization') as spawn:
            with self.assertRaises(InputError):
                start_description_optimization(a, self.user)
        spawn.assert_not_called()
        self.assertFalse(ActivityDescriptionTask.objects.filter(activity=a).exists())

    def test_start_while_running_raises_busy(self):
        a = self._act()
        self._task(a, status='running')
        with patch('activities.services._spawn_optimization') as spawn:
            with self.assertRaises(OptimizeBusyError):
                start_description_optimization(a, self.user)
        spawn.assert_not_called()
        self.assertEqual(ActivityDescriptionTask.objects.filter(activity=a, status='running').count(), 1)

    def test_db_partial_unique_blocks_second_running_only(self):
        a = self._act()
        self._task(a, status='running')
        # 第二个 running 撞 partial unique；savepoint 包住以免污染外层事务
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ActivityDescriptionTask.objects.create(
                    activity=a, user=self.user, status='running', original='y')
        # done/error 不受约束限制（同活动可有多条历史任务）
        ActivityDescriptionTask.objects.create(activity=a, user=self.user, status='done', original='z')
        self.assertEqual(ActivityDescriptionTask.objects.filter(activity=a).count(), 2)

    def test_start_race_integrity_error_becomes_busy(self):
        # 先读后写窗口里另一请求刚建了 running：模拟 create 撞约束 → Busy（不是 500）
        a = self._act()
        with patch('activities.services.active_description_task', return_value=None), \
             patch('activities.services.ActivityDescriptionTask.objects.create',
                   side_effect=IntegrityError('dup')), \
             patch('activities.services._spawn_optimization') as spawn:
            with self.assertRaises(OptimizeBusyError):
                start_description_optimization(a, self.user)
        spawn.assert_not_called()

    # ── 服务层：僵尸回收 ──

    def test_active_task_keeps_fresh_running(self):
        a = self._act()
        task = self._task(a, status='running')
        self.assertEqual(active_description_task(a).id, task.id)

    def test_active_task_reclaims_stale_running(self):
        a = self._act()
        task = self._task(a, status='running')
        old_ts = timezone.now() - timedelta(seconds=DESC_OPTIMIZE_STALE_SECONDS + 60)
        ActivityDescriptionTask.objects.filter(id=task.id).update(updated_at=old_ts)
        self.assertIsNone(active_description_task(a))
        task.refresh_from_db()
        self.assertEqual(task.status, 'error')
        self.assertTrue(Notification.objects.filter(user=self.user, kind='desc_optimize_failed').exists())

    def test_active_task_none_when_no_running(self):
        a = self._act()
        self._task(a, status='done')
        self.assertIsNone(active_description_task(a))

    # ── 后台 worker ──

    def test_worker_success_writes_description_logs_and_notifies(self):
        a = self._act(description='原始的描述文字')
        task = self._task(a, status='running')
        with patch('core.ai.ai_round_trip', return_value='优化后的描述文字') as ai, \
             patch('activities.services.connection') as conn:
            run_description_optimization(task.id)
        ai.assert_called_once()
        self.assertEqual(ai.call_args.kwargs.get('purpose'), 'content')
        a.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(a.description, '优化后的描述文字')
        self.assertEqual(task.status, 'done')
        self.assertEqual(task.result, '优化后的描述文字')
        self.assertTrue(ActivityLog.objects.filter(
            activity=a, action='edited', summary__startswith='AI 优化').exists())
        n = Notification.objects.filter(user=self.user, kind='desc_optimized').first()
        self.assertIsNotNone(n)
        self.assertIn(a.name, n.title)
        self.assertEqual(n.link, reverse('activities:activity_detail', args=[a.id]))
        conn.close.assert_called()

    def test_worker_cleans_ai_output_before_write(self):
        a = self._act(description='旧')
        task = self._task(a, status='running')
        with patch('core.ai.ai_round_trip', return_value='```\n优化后的描述：干净正文\n```'), \
             patch('activities.services.connection'):
            run_description_optimization(task.id)
        a.refresh_from_db()
        self.assertEqual(a.description, '干净正文')

    def test_worker_failure_keeps_description_and_notifies(self):
        a = self._act(description='不能动的原描述')
        task = self._task(a, status='running')
        with patch('core.ai.ai_round_trip', return_value=None), \
             patch('activities.services.connection'):
            run_description_optimization(task.id)
        a.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(a.description, '不能动的原描述')
        self.assertEqual(task.status, 'error')
        self.assertTrue(Notification.objects.filter(user=self.user, kind='desc_optimize_failed').exists())
        self.assertFalse(Notification.objects.filter(user=self.user, kind='desc_optimized').exists())

    def test_worker_ai_exception_is_failure_not_crash(self):
        a = self._act(description='原描述')
        task = self._task(a, status='running')
        with patch('core.ai.ai_round_trip', side_effect=RuntimeError('boom')), \
             patch('activities.services.connection') as conn:
            run_description_optimization(task.id)   # 不得抛出
        task.refresh_from_db()
        self.assertEqual(task.status, 'error')
        a.refresh_from_db()
        self.assertEqual(a.description, '原描述')
        conn.close.assert_called()

    def test_worker_empty_result_is_failure(self):
        a = self._act(description='原描述')
        task = self._task(a, status='running')
        with patch('core.ai.ai_round_trip', return_value='   '), \
             patch('activities.services.connection'):
            run_description_optimization(task.id)
        task.refresh_from_db()
        self.assertEqual(task.status, 'error')
        a.refresh_from_db()
        self.assertEqual(a.description, '原描述')

    def test_worker_aborts_if_task_no_longer_running(self):
        a = self._act(description='原描述')
        task = self._task(a, status='running')
        ActivityDescriptionTask.objects.filter(id=task.id).update(status='error')
        with patch('core.ai.ai_round_trip', return_value='不该写回') as ai, \
             patch('activities.services.connection'):
            run_description_optimization(task.id)
        ai.assert_not_called()
        a.refresh_from_db()
        self.assertEqual(a.description, '原描述')

    # ── 端点：发起优化 ──

    def test_optimize_endpoint_starts_task(self):
        a = self._act()
        with patch('activities.services._spawn_optimization'):
            resp = self.client.post(self._opt_url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['optimizing'])
        self.assertEqual(data['status_url'], self._status_url(a))
        self.assertTrue(ActivityDescriptionTask.objects.filter(activity=a, status='running').exists())

    def test_optimize_endpoint_empty_description_400(self):
        a = self._act(description='')
        with patch('activities.services._spawn_optimization'):
            resp = self.client.post(self._opt_url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])

    def test_optimize_endpoint_busy_409(self):
        a = self._act()
        self._task(a, status='running')
        resp = self.client.post(self._opt_url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()['optimizing'])

    def test_optimize_endpoint_cross_user_json_404(self):
        other = User.objects.create_user('otheropt', password='p')
        a = Activity.objects.create(user=other, name='别人的', status='planned',
                                    start_date=self.today, description='私密')
        resp = self.client.post(self._opt_url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())
        self.assertFalse(ActivityDescriptionTask.objects.filter(activity=a).exists())

    def test_optimize_endpoint_anonymous_401(self):
        a = self._act()
        self.client.logout()
        resp = self.client.post(self._opt_url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.json().get('reauth'))

    def test_optimize_endpoint_get_405(self):
        a = self._act()
        resp = self.client.get(self._opt_url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 405)

    # ── 端点：状态轮询 ──

    def test_status_running(self):
        a = self._act()
        self._task(a, status='running')
        data = self.client.get(self._status_url(a), HTTP_ACCEPT='application/json').json()
        self.assertEqual(data['state'], 'running')
        self.assertTrue(data['optimizing'])

    def test_status_done_returns_description(self):
        a = self._act(description='优化后的全文')
        self._task(a, status='done', result='优化后的全文')
        data = self.client.get(self._status_url(a), HTTP_ACCEPT='application/json').json()
        self.assertEqual(data['state'], 'done')
        self.assertFalse(data['optimizing'])
        self.assertEqual(data['description'], '优化后的全文')

    def test_status_error_returns_message(self):
        a = self._act(description='原描述')
        self._task(a, status='error', error='优化失败，请稍后重试')
        data = self.client.get(self._status_url(a), HTTP_ACCEPT='application/json').json()
        self.assertEqual(data['state'], 'error')
        self.assertFalse(data['optimizing'])
        self.assertEqual(data['error'], '优化失败，请稍后重试')
        self.assertEqual(data['description'], '原描述')

    def test_status_idle_when_no_task(self):
        a = self._act()
        data = self.client.get(self._status_url(a), HTTP_ACCEPT='application/json').json()
        self.assertEqual(data['state'], 'idle')
        self.assertFalse(data['optimizing'])

    def test_status_cross_user_json_404(self):
        other = User.objects.create_user('otheropt2', password='p')
        a = Activity.objects.create(user=other, name='别人的', status='planned',
                                    start_date=self.today, description='私密')
        resp = self.client.get(self._status_url(a), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.json())

    def test_status_reclaims_stale_running(self):
        a = self._act()
        task = self._task(a, status='running')
        old_ts = timezone.now() - timedelta(seconds=DESC_OPTIMIZE_STALE_SECONDS + 60)
        ActivityDescriptionTask.objects.filter(id=task.id).update(updated_at=old_ts)
        data = self.client.get(self._status_url(a), HTTP_ACCEPT='application/json').json()
        self.assertEqual(data['state'], 'error')   # 僵尸回收后不再是 running
        self.assertFalse(data['optimizing'])

    # ── 端点：编辑锁 ──

    def test_edit_get_includes_optimizing_flag_and_urls(self):
        a = self._act()
        data = self.client.get(self._edit_url(a), HTTP_ACCEPT='application/json').json()
        self.assertFalse(data['optimizing'])
        self.assertEqual(data['optimize_url'], self._opt_url(a))
        self.assertEqual(data['optimize_status_url'], self._status_url(a))

    def test_edit_get_optimizing_true_when_running(self):
        a = self._act()
        self._task(a, status='running')
        data = self.client.get(self._edit_url(a), HTTP_ACCEPT='application/json').json()
        self.assertTrue(data['optimizing'])

    def test_edit_post_locked_409_while_running(self):
        a = self._act(description='锁定中的原描述')
        self._task(a, status='running')
        resp = self.client.post(self._edit_url(a), {'description': '想篡改'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()['optimizing'])
        a.refresh_from_db()
        self.assertEqual(a.description, '锁定中的原描述')   # 优化中绝不落库

    def test_edit_post_allowed_after_done(self):
        a = self._act(description='优化后的描述')
        self._task(a, status='done', result='优化后的描述')
        resp = self.client.post(self._edit_url(a), {'description': '继续手动编辑'},
                                HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.description, '继续手动编辑')

    # ── 表单页描述锁 ──

    def test_form_edit_description_readonly_while_running(self):
        a = self._act()
        self._task(a, status='running')
        html = self.client.get(reverse('activities:activity_edit', args=[a.id])).content.decode()
        self.assertIn('AI 正在优化描述，完成后可编辑', html)
        self.assertIn('readonly', html)

    def test_form_post_keeps_description_while_running(self):
        a = self._act(description='原始描述', name='旧名字')
        self._task(a, status='running')
        resp = self.client.post(reverse('activities:activity_edit', args=[a.id]), {
            'name': '新名字', 'status': 'planned', 'description': '想篡改的描述',
            'start_date': self.today.isoformat(),
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.description, '原始描述')   # 描述被锁，其余字段照常保存
        self.assertEqual(a.name, '新名字')
        self.assertIn('本次未改动描述', resp.content.decode())

    # ── 接线：弹窗 ──

    def test_modal_has_ai_button_and_optimizing_banner(self):
        for el_id in ('desc-edit-ai', 'desc-edit-ai-label', 'desc-edit-optimizing'):
            self.assertIn(f'id="{el_id}"', self.modal)
        self.assertIn('AI优化', self.modal)
        self.assertIn('role="status"', self.modal)
        self.assertIn('aria-live="polite"', self.modal)
        self.assertIn('desc-spinner', self.modal)
        self.assertIn('disabled:opacity-60', self.modal)   # textarea 锁定态

    def test_modal_ai_button_does_not_use_htmx(self):
        self.assertNotIn('hx-', code_only(self.modal))

    # ── 接线：JS ──

    def test_js_has_three_optimize_functions(self):
        for fn in ('function setOptimizing', 'function startOptimize', 'function pollOptimize'):
            self.assertIn(fn, self.js_code)

    def test_js_setoptimizing_locks_input_submit_and_ai(self):
        body = self.js_code[self.js_code.index('function setOptimizing'):
                            self.js_code.index('function startOptimize')]
        self.assertIn('editInput.disabled = on', body)
        self.assertIn('editSubmit.disabled = on', body)
        self.assertIn('aiBtn.disabled = on', body)
        self.assertIn("'优化中'", body)

    def test_js_start_optimize_uses_shared_outlet_and_csrf(self):
        body = self.js_code[self.js_code.index('function startOptimize'):
                            self.js_code.index('function pollOptimize')]
        self.assertIn('window.paJsonFetch', body)
        self.assertIn("method: 'POST'", body)
        self.assertIn("'X-CSRFToken': csrfToken()", body)
        self.assertIn('r.status === 409', body)   # 忙不当错误，恢复轮询

    def test_js_poll_handles_running_done_error_and_broadcasts(self):
        body = self.js_code[self.js_code.index('function pollOptimize'):
                            self.js_code.index('if (modal) {')]
        self.assertIn("d.state === 'running'", body)
        self.assertIn("d.state === 'done'", body)
        self.assertIn('setTimeout(pollOptimize', body)
        self.assertIn('updateCardDesc(id', body)
        self.assertIn("document.dispatchEvent(new CustomEvent('notifications:changed'))", body)
        self.assertIn("document.dispatchEvent(new CustomEvent('activities:changed'))", body)

    def test_js_poll_cap_unlocks(self):
        self.assertIn('optimizePolls > 90', self.js_code)

    def test_js_ai_button_wired_to_start(self):
        self.assertIn("aiBtn.addEventListener('click', startOptimize)", self.js_code)

    def test_js_optimize_does_not_add_global_keydown(self):
        self.assertNotIn("document.addEventListener('keydown'", self.js_code)

    # ── 接线：CSS ──

    def test_css_spinner_defined_with_reduced_motion(self):
        self.assertIn('.desc-spinner', self.css)
        self.assertIn('@keyframes desc-spin', self.css)
        self.assertIn('prefers-reduced-motion', self.css)
