"""
每日 AI 建议预计算：为所有活跃用户生成今日建议并缓存

cron 每日 00:00 执行，幂等（upsert by user+date）。
AI 超时或失败时降级为规则模板。
"""
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


def _generate_suggestion_for_user(user, today=None):
    """为单个用户生成今日建议，返回 (suggestion_text, is_ai)"""
    from activities.models import Activity, DailySuggestion
    from activities.views.daily_views import _detect_conflicts

    if today is None:
        today = timezone.localdate()

    qs = Activity.objects.filter(user=user).prefetch_related('tags', 'participants')

    # 复用 daily_view 的分组逻辑
    ongoing = list(qs.filter(
        start_date__lte=today,
    ).filter(
        models.Q(end_date__gte=today) | models.Q(end_date__isnull=True, start_date=today)
    ).exclude(status='cancelled').exclude(status='done'))

    starting_today = list(qs.filter(start_date=today).exclude(
        status='cancelled'
    ).exclude(
        status='done'
    ).exclude(id__in=[a.id for a in ongoing]))

    overdue_rolled_in = list(qs.filter(
        status='planned',
        start_date__lt=today,
    ).order_by('start_date')[:10])

    all_today = [*ongoing, *starting_today, *overdue_rolled_in]
    conflict_ids = _detect_conflicts([*ongoing, *starting_today])

    if not all_today:
        return ('今天没有待处理的活动，享受轻松的一天吧', False)

    # 尝试 AI 生成
    summary_lines = [f'- {a.name}（{a.get_status_display()}）' for a in all_today[:10]]
    try:
        from core.ai import ai_round_trip
        reply = ai_round_trip(
            '以下是用户今天的活动数据，请用不超过 80 字的中文给出一条可执行建议，'
            '突出优先级最高的一件事，口语化，不要列表不要寒暄：\n'
            + '\n'.join(summary_lines),
            timeout=60, purpose='general')
        if reply:
            suggestion = reply.strip().splitlines()[0][:200]
            return (suggestion, True)
    except Exception as exc:
        logger.warning('用户 %s AI 建议生成失败: %s', user.username, exc)

    # 规则降级
    if overdue_rolled_in:
        return (f'有 {len(overdue_rolled_in)} 个活动已过期，建议先处理最紧急的', False)
    elif conflict_ids:
        return (f'今天有 {len(conflict_ids)} 个活动时间冲突，注意调整', False)
    elif ongoing:
        return (f'当前有 {len(ongoing)} 个活动进行中，专注完成它们', False)
    else:
        return ('今天有新活动开始，记得跟进进度', False)


class Command(BaseCommand):
    help = '为所有活跃用户预计算今日 AI 建议并缓存'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date', type=str, default=None,
            help='指定日期（YYYY-MM-DD），默认今天')

    def handle(self, *args, **options):
        from activities.models import DailySuggestion

        User = get_user_model()
        if options['date']:
            from datetime import date as date_type
            today = date_type.fromisoformat(options['date'])
        else:
            today = timezone.localdate()

        users = User.objects.filter(is_active=True)
        total = 0
        ai_count = 0

        for user in users:
            try:
                suggestion, is_ai = _generate_suggestion_for_user(user, today)
                DailySuggestion.objects.update_or_create(
                    user=user, date=today,
                    defaults={'suggestion': suggestion, 'is_ai': is_ai},
                )
                total += 1
                if is_ai:
                    ai_count += 1
                self.stdout.write(
                    f'  {user.username}: {"[AI]" if is_ai else "[规则]"} {suggestion[:50]}...'
                )
            except Exception as exc:
                logger.warning('用户 %s 建议生成失败: %s', user.username, exc)

        self.stdout.write(
            self.style.SUCCESS(
                f'完成：{today} 共 {total} 条建议（AI: {ai_count}, 规则: {total - ai_count}）'
            )
        )
