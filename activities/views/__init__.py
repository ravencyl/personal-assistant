"""activities 视图包：按功能域拆分，路由与外部引用统一从本包取视图函数

拆分前是单文件 views.py（1500+ 行），现按域分模块：
- _common            视图层共享小工具（标签建议 / 问候语 / 费用标注）
- quick_input_views  一句话快速输入（AI 解析 + 降级规则）与快速创建
- list_views         活动列表（树形 + 筛选 + 排序 + 分页）
- detail_views       活动详情及其子资源（子任务 / 评论 / 状态 / 附件）
- form_views         独立创建 / 编辑 / 删除
- expense_views      费用记一笔 / 编辑 / 删除 / 报告 / 图表
- calendar_views     日历页面 / 数据 API / ICS 订阅源
- daily_views        Daily 每日简报与下一步行动

外部入口保持稳定：activities/urls.py 的 `from . import views`、
personal_assistant/urls.py 的 `from activities.views import daily_view, calendar_feed`、
tests 的 `from activities.views import _week_anchor_text` 均不受影响。
"""
from ._common import attach_costs  # noqa: F401
from .quick_input_views import (  # noqa: F401
    _ai_parse,
    _week_anchor_text,
    activity_quick_create,
    ocr_receipt_view,
    parse_quick_input_view,
)
from .list_views import activity_list  # noqa: F401
from .detail_views import (  # noqa: F401
    _parse_date_input,
    _split_name_input,
    _subactivity_timeline,
    activity_comment_add,
    activity_comment_delete,
    activity_detail,
    activity_quick_sub,
    activity_set_status,
    add_subactivity,
    attachment_delete,
    attachment_upload,
    blocked_add,
    blocked_remove,
    blocked_search,
    subactivity_manual_create,
)
from .form_views import activity_create, activity_delete, activity_edit  # noqa: F401
from .expense_views import (  # noqa: F401
    expense_chart_data,
    expense_create,
    expense_delete,
    expense_edit,
    expense_heatmap_data,
    expense_report,
)
from .calendar_views import (  # noqa: F401
    activity_calendar,
    calendar_data,
    calendar_feed,
    calendar_feed_settings,
    move_date_view,
)
from .daily_views import daily_view, next_actions, refresh_suggestion  # noqa: F401
from .archive_views import archive_list, archive_activity, unarchive_activity  # noqa: F401
from .timeline_views import timeline_view  # noqa: F401
from .template_views import (  # noqa: F401
    template_list,
    template_create,
    template_use,
)
from .batch_views import batch_update_view  # noqa: F401
from .dependency_views import dependency_data  # noqa: F401
from .export_views import activity_export_csv, expense_export_csv  # noqa: F401

__all__ = [
    'attach_costs',
    '_ai_parse', '_week_anchor_text', '_parse_date_input', '_split_name_input',
    '_subactivity_timeline',
    'parse_quick_input_view', 'activity_quick_create', 'ocr_receipt_view', 'activity_list',
    'activity_detail', 'activity_set_status', 'add_subactivity',
    'activity_comment_add', 'activity_comment_delete', 'activity_quick_sub',
    'subactivity_manual_create', 'attachment_upload', 'attachment_delete',
    'activity_create', 'activity_edit', 'activity_delete',
    'expense_create', 'expense_edit', 'expense_delete',
    'expense_report', 'expense_chart_data', 'expense_heatmap_data',
    'activity_calendar', 'calendar_data', 'calendar_feed', 'calendar_feed_settings',
    'daily_view', 'next_actions', 'refresh_suggestion',
    'archive_list', 'archive_activity', 'unarchive_activity',
    'timeline_view',
    'template_list', 'template_create', 'template_use',
]
