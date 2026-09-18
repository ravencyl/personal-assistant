"""月度洞察：把上个月的活动/费用汇总成一条「这个月你在忙什么」的记忆

cron 每月 1 日早上执行（建议 0 7 1 * *）。幂等与防垃圾三层：
- 同月已写过（内容以【YYYY-MM 月度回顾】开头）→ 跳过
- 上月无任何活动 → 跳过（没数据硬生成就是占位垃圾）
- _is_similar_content 命中 → 跳过（记忆库被垃圾填满比少一条记忆糟得多）

AI 失败降级规则模板，importance 固定 4（索引型，不抢偏好/目标的注入位）。
"""
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


def _month_prefix(ym):
    return f'【{ym} 月度回顾】'


def _collect_stats(user, month_start, month_end):
    """上月个人指标（花费/完成数按 user 过滤——个人指标口径，不混他人数据）"""
    from activities.models import Activity, Expense
    from core.tags import tag_names

    acts = Activity.objects.filter(
        user=user, start_date__gte=month_start, start_date__lte=month_end,
    )
    total = acts.count()
    done = acts.filter(status='done').count()
    expense = Expense.objects.filter(
        user=user, paid_at__gte=month_start, paid_at__lte=month_end,
    ).aggregate(s=Sum('amount'))['s'] or 0

    # 话题标签：按上月活动标签出现频次取 top 3
    tag_count = {}
    for a in acts.prefetch_related('tags')[:100]:
        for name in tag_names(a):
            tag_count[name] = tag_count.get(name, 0) + 1
    top_tags = [t for t, _ in sorted(tag_count.items(), key=lambda x: -x[1])[:3]]

    return {'total': total, 'done': done, 'expense': expense, 'top_tags': top_tags}


def _fallback_insight(stats):
    tags = '、'.join(stats['top_tags']) if stats['top_tags'] else '无特定主题'
    return (f"完成 {stats['done']}/{stats['total']} 个活动，"
            f"花费 ¥{stats['expense']:.0f}，主要话题：{tags}。")


def generate_monthly_insight(user, month_start, month_end, ym):
    """为单个用户生成上月洞察记忆。返回 'saved' / 'skip:<原因>'"""
    from memory.models import Memory
    from memory.services import _is_similar_content

    prefix = _month_prefix(ym)
    if Memory.objects.filter(user=user, content__startswith=prefix).exists():
        return 'skip:already'

    stats = _collect_stats(user, month_start, month_end)
    if stats['total'] == 0:
        return 'skip:empty'

    prompt = (
        '以下是我上个月的活动统计数据，请写一条 1-2 句话的中文洞察'
        '（不超过 120 字，讲清这个月的主线是什么、和以往相比值得注意什么，'
        '不要客套不要罗列数字）：\n'
        f"- 活动总数：{stats['total']}，其中完成 {stats['done']} 个\n"
        f"- 总花费：¥{stats['expense']:.0f}\n"
        f"- 主要话题标签：{'、'.join(stats['top_tags']) or '无'}\n"
    )
    try:
        from core.ai import ai_round_trip
        text = ai_round_trip(prompt, timeout=60, purpose='general')
    except Exception as exc:
        logger.warning('月度洞察 AI 调用失败（降级规则模板）: %s', exc)
        text = None
    if not text:
        text = _fallback_insight(stats)

    content = f'{prefix} {" ".join(text.split())}'[:500]
    if _is_similar_content(user, content):
        return 'skip:similar'

    Memory.objects.create(user=user, content=content, category='fact', importance=4)
    return 'saved'


class Command(BaseCommand):
    help = '为所有有上月数据的用户生成月度洞察并沉淀为记忆（每月 1 日 cron）'

    def handle(self, *args, **options):
        User = get_user_model()
        today = timezone.localdate()
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        month_start = last_month_end.replace(day=1)
        ym = f'{month_start.year}-{month_start.month:02d}'

        saved = 0
        for user in User.objects.filter(is_active=True):
            try:
                result = generate_monthly_insight(user, month_start, last_month_end, ym)
            except Exception:
                logger.exception('月度洞察失败: user=%s', user.username)
                continue
            if result == 'saved':
                saved += 1
                self.stdout.write(f'  {user.username}: 已保存')
            else:
                self.stdout.write(f'  {user.username}: {result}')

        self.stdout.write(self.style.SUCCESS(f'月度洞察完成: 保存 {saved} 条 ({ym})'))
