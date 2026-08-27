"""每日晚间执行：为每个活跃用户预生成当日摘要（AI 优先 + 规则兜底，幂等）

用法：
    python manage.py generate_daily_summary [--dry-run]

幂等规则：当日已存在 ready/fallback 记录则跳过，可安全重跑。
"""
import json
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import DailySummary
from core.report_generator import ai_round_trip

logger = logging.getLogger(__name__)


def collect_daily_stats(user, today):
    """聚合用户当日数据：做了什么 / 花了多少 / 明天有什么"""
    from django.db.models import Sum

    from activities.models import Activity, Expense

    tomorrow = today + timedelta(days=1)

    done_today = list(
        Activity.objects.filter(user=user, status='done', end_date=today)
        .order_by('name').values_list('name', flat=True)
    )
    in_progress = list(
        Activity.objects.filter(user=user, status='in_progress')
        .order_by('-start_date').values_list('name', flat=True)[:20]
    )

    expenses = Expense.objects.filter(user=user, paid_at=today)
    total_expense = float(expenses.aggregate(s=Sum('amount'))['s'] or 0)
    cat_labels = dict(Expense.CATEGORY_CHOICES)
    expense_by_category = {
        cat_labels.get(cat, cat): float(amount)
        for cat, amount in expenses.values('category')
        .annotate(s=Sum('amount')).values_list('category', 's')
    }

    tomorrow_qs = Activity.objects.filter(
        user=user, start_date=tomorrow
    ).exclude(status='cancelled').order_by('name')
    tomorrow_planned = list(
        tomorrow_qs.filter(recurring_source__isnull=True).values_list('name', flat=True)
    )
    tomorrow_habits = list(
        tomorrow_qs.filter(recurring_source__isnull=False).values_list('name', flat=True)
    )

    return {
        'date': today.isoformat(),
        'done_today': done_today,
        'in_progress': in_progress,
        'total_expense': total_expense,
        'expense_by_category': expense_by_category,
        'tomorrow_planned': tomorrow_planned,
        'tomorrow_habits': tomorrow_habits,
    }


def build_prompt(stats):
    """构造 AI 摘要提示词"""
    return f"""以下是用户 {stats['date']} 这一天的数据，请生成一段晚间摘要。

要求：
1. 150-250 字，中文，自然亲切，直接输出正文
2. 不要标题、不要 Markdown 符号、不要列表符号，用自然段分隔
3. 先简短回顾今天（完成的事、进行中的进展、消费情况），再提醒明天的安排，最后一句轻松收尾

数据：
{json.dumps(stats, ensure_ascii=False, indent=2)}"""


def build_fallback(stats):
    """AI 失败时的纯数据规则模板"""
    lines = []

    if stats['done_today']:
        lines.append(f"今天完成了 {len(stats['done_today'])} 项活动：{'、'.join(stats['done_today'])}。")
    else:
        lines.append('今天没有完成的活动记录。')

    if stats['in_progress']:
        lines.append(f"还有 {len(stats['in_progress'])} 项活动进行中：{'、'.join(stats['in_progress'][:5])}。")

    if stats['total_expense'] > 0:
        cats = '、'.join(
            f'{label} ¥{amount:.0f}'
            for label, amount in sorted(stats['expense_by_category'].items(), key=lambda x: -x[1])
        )
        lines.append(f"今日共消费 ¥{stats['total_expense']:.0f}（{cats}）。")
    else:
        lines.append('今日没有消费记录。')

    tomorrow = stats['tomorrow_planned'] + stats['tomorrow_habits']
    if tomorrow:
        lines.append(f"明天有 {len(tomorrow)} 项安排：{'、'.join(tomorrow[:5])}，记得提前准备。")
    else:
        lines.append('明天暂时没有安排，可以放松一下。')

    return '\n'.join(lines)


class Command(BaseCommand):
    help = '为每个活跃用户生成当日晚间摘要（AI 优先，失败降级为规则模板，幂等可重跑）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='仅显示将处理的用户与数据概况，不调用 AI、不写库',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        today = timezone.localdate()

        from activities.models import Activity, Expense
        User = get_user_model()
        user_ids = set(Activity.objects.values_list('user_id', flat=True))
        user_ids |= set(Expense.objects.values_list('user_id', flat=True))
        users = User.objects.filter(is_active=True, id__in=user_ids)

        created = updated = skipped = failed = 0
        for user in users:
            try:
                stats = collect_daily_stats(user, today)
            except Exception as e:
                logger.error(f'每日摘要数据聚合失败 user={user.id}: {e}')
                failed += 1
                continue

            existing = DailySummary.objects.filter(
                user=user, summary_date=today
            ).first()

            if dry_run:
                action = '跳过（当日已有摘要）' if existing and existing.status != 'pending' \
                    else ('更新' if existing else '新建')
                self.stdout.write(
                    f'[dry-run] {user.username}: 完成 {len(stats["done_today"])} / '
                    f'进行中 {len(stats["in_progress"])} / '
                    f'消费 ¥{stats["total_expense"]:.0f} / '
                    f'明日 {len(stats["tomorrow_planned"]) + len(stats["tomorrow_habits"])} 项 → {action}'
                )
                continue

            # 幂等：当日已存在 ready/fallback 记录则跳过
            if existing and existing.status != 'pending':
                skipped += 1
                continue

            content, status = self._generate(stats)
            if existing:
                existing.content = content
                existing.stats = stats
                existing.status = status
                existing.generated_at = timezone.now()
                existing.save()
                updated += 1
            else:
                DailySummary.objects.create(
                    user=user,
                    summary_date=today,
                    content=content,
                    stats=stats,
                    status=status,
                    generated_at=timezone.now(),
                )
                created += 1

        msg = f'完成：新建 {created}，更新 {updated}，跳过 {skipped}'
        if failed:
            msg += f'，失败 {failed}'
        if dry_run:
            self.stdout.write('[dry-run] 未写入数据库')
        self.stdout.write(msg)

    def _generate(self, stats):
        """AI 生成，失败降级为规则模板。返回 (content, status)"""
        try:
            result = ai_round_trip(build_prompt(stats), timeout=90)
        except Exception as e:
            logger.warning(f'每日摘要 AI 生成失败: {e}')
            result = None

        if result and result.strip():
            return result.strip(), 'ready'
        return build_fallback(stats), 'fallback'
