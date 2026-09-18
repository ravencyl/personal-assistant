"""
行为模式分析：从活动历史中提取规律性洞察

结果存为 Memory(category='habit')，同时在 daily 页面的建议区展示。
所有函数幂等、无副作用（只读分析 + 返回结构化数据）。
"""
import logging
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def analyze_activity_patterns(user):
    """分析过去 30 天的活动模式

    返回:
        {
            'weekday_distribution': {0: 5, 1: 3, ...},  # 星期几创建活动最多
            'completion_rates': {'planned': 10, 'done': 20, ...},  # 各状态数量
            'insights': ['周三活动完成率最高 80%', ...],  # 结构化洞察文案
        }
    """
    from activities.models import Activity

    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)

    # 过去 30 天的活动
    activities = Activity.objects.filter(
        user=user,
        archived_at__isnull=True,
        created_at__gte=thirty_days_ago,
    )

    if not activities.exists():
        return {'weekday_distribution': {}, 'completion_rates': {}, 'insights': []}

    # 1. 按星期几分布（创建日）
    weekday_counter = Counter()
    for a in activities.values_list('created_at', flat=True):
        if a:
            weekday_counter[a.weekday()] += 1

    # 2. 状态分布
    status_counter = Counter()
    for s, in activities.values_list('status'):
        status_counter[s] += 1

    total = sum(status_counter.values()) or 1
    done_count = status_counter.get('done', 0)

    # 3. 生成洞察
    insights = []

    # 最高产的一天
    if weekday_counter:
        best_day = weekday_counter.most_common(1)[0]
        day_name = WEEKDAY_NAMES[best_day[0]]
        insights.append(f'{day_name}创建的活动最多（{best_day[1]} 个）')

    # 完成率
    completion_rate = int(done_count / total * 100)
    if total >= 3:
        if completion_rate >= 70:
            insights.append(f'近 30 天活动完成率 {completion_rate}%，表现不错')
        elif completion_rate >= 40:
            insights.append(f'近 30 天活动完成率 {completion_rate}%，还有提升空间')
        else:
            insights.append(f'近 30 天活动完成率 {completion_rate}%，可以考虑减少计划量')

    # 按星期几的完成率
    weekday_done = defaultdict(int)
    weekday_total = defaultdict(int)
    for a in activities.filter(start_date__gte=thirty_days_ago.date()):
        if a.start_date:
            wd = a.start_date.weekday()
            weekday_total[wd] += 1
            if a.status == 'done':
                weekday_done[wd] += 1

    best_completion_day = None
    best_rate = 0
    for wd, cnt in weekday_total.items():
        if cnt >= 2:
            rate = weekday_done[wd] / cnt
            if rate > best_rate:
                best_rate = rate
                best_completion_day = wd

    if best_completion_day is not None and best_rate > 0.5:
        day_name = WEEKDAY_NAMES[best_completion_day]
        pct = int(best_rate * 100)
        insights.append(f'{day_name}活动完成率最高 {pct}%')

    return {
        'weekday_distribution': dict(weekday_counter),
        'completion_rates': dict(status_counter),
        'insights': insights[:3],  # 最多 3 条
    }


def save_pattern_insights(user):
    """将行为模式洞察存为 Memory(category='habit')

    幂等：先删旧的 habit 类模式记忆，再存新的。
    """
    from memory.models import Memory

    patterns = analyze_activity_patterns(user)
    if not patterns['insights']:
        return []

    # 清除旧的模式洞察（保留用户手动创建的 habit 类记忆）
    Memory.objects.filter(
        user=user,
        category='habit',
        content__startswith='[模式]',
    ).delete()

    saved = []
    for insight in patterns['insights']:
        mem = Memory.objects.create(
            user=user,
            content=f'[模式] {insight}',
            category='habit',
            importance=4,
        )
        saved.append(mem)

    logger.info('用户 %s 保存了 %d 条行为模式洞察', user.username, len(saved))
    return saved
