"""视图层共享的小工具（跨拆分模块使用）"""
from django.db.models import Count
from django.utils import timezone

from core.tags import tag_suggestions

from ..models import Expense
from ..utils import expense_totals_map


def _user_tag_names(user):
    """autocomplete 建议源：预建启用标签在前 + 用户用过的补后（core.Tag scope 口径）"""
    return tag_suggestions('activity', user)


def _participant_skip_text(skipped):
    """自动识别未命中参与者的单行提示（给前端 toast 用，无跳过时为空串）"""
    if not skipped:
        return ''
    return f"参与者「{'、'.join(skipped)}」不存在，未添加"


def _greeting():
    """按时段问候语（活动列表与 Daily 共用同一份口径）"""
    hour = timezone.localtime().hour
    if hour < 6:
        return '夜深了，早点休息'
    if hour < 12:
        return '早上好'
    if hour < 14:
        return '中午好'
    if hour < 18:
        return '下午好'
    return '晚上好'


def attach_costs(activities):
    """为活动列表附加费用合计/笔数/预算标注（避免 N+1）。"""
    ids = [a.id for a in activities]
    if ids:
        totals = expense_totals_map(ids)
        counts = dict(
            Expense.objects.filter(activity_id__in=ids)
            .values_list('activity_id').annotate(cnt=Count('id'))
            .values_list('activity_id', 'cnt')
        )
    else:
        totals = {}
        counts = {}
    for a in activities:
        a.expense_total = float(totals.get(a.id, 0) or 0)
        a.expense_count = counts.get(a.id, 0)
    return activities
