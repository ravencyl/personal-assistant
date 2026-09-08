"""Daily 页顶部区（子任务分组）生成器——纯规则、零 AI、不缓存

由 daily_view 每次请求调用一次，避免重复查询。
"""


def generate_daily_plan(user):
    """生成 Daily 页「子任务」结构化数据（纯规则，零 AI）

    只放下面活动卡片区覆盖不到的信息；今日发生的活动本身不在此列出，
    避免与 daily_view 的「今日进行中」卡片重复（同一活动上下各出现一次）。

    返回 dict：
    - subtask_groups: 未完成子活动 Top 5，按父活动分组
    - is_empty:       分组为空
    """
    from activities.models import Activity

    children = Activity.objects.filter(
        user=user,
        parent__isnull=False,
        status__in=('planned', 'in_progress'),
    ).select_related('parent').order_by('parent_id', 'start_date', 'id')[:5]
    subtask_groups = []
    for child in children:
        if subtask_groups and subtask_groups[-1]['parent'].id == child.parent_id:
            subtask_groups[-1]['children'].append(child)
        else:
            subtask_groups.append({'parent': child.parent, 'children': [child]})

    return {
        'subtask_groups': subtask_groups,
        'is_empty': not subtask_groups,
    }
