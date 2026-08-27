"""每日早间执行：为每个活跃用户预生成个性化洞察（AI 优先 + 规则兜底，幂等）

用法：
    python manage.py generate_daily_insights [--dry-run]

幂等规则：当日已存在 ready/fallback 记录则跳过，可安全重跑。
调度：每日 06:30（早于用户首次访问，洞察全天可见）
"""
import json
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import DailyInsight
from core.report_generator import ai_round_trip

logger = logging.getLogger(__name__)

# AI 生成的 action.url 白名单
URL_WHITELIST = frozenset([
    '/',
    '/activities/',
    '/activities/expense-report/',
    '/activities/recurring/',
    '/activities/next-actions/',
    '/reports/weekly/',
])

ICON_WHITELIST = frozenset(['goal', 'expense', 'habit', 'plan', 'calendar', 'alert'])


def collect_insight_data(user, today):
    """聚合用户近期行为数据，作为 AI 洞察的输入"""
    from django.db.models import Sum

    from activities.models import Activity, Expense
    from core.suggestions import compute_habit_streaks

    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)

    # 本周完成活动
    done_this_week = list(
        Activity.objects.filter(
            user=user, status='done',
            end_date__gte=week_start, end_date__lte=today,
        ).order_by('name').values_list('name', flat=True)[:10]
    )

    # 本周投入时间（预估）
    time_this_week = Activity.objects.filter(
        user=user, status='done',
        end_date__gte=week_start, end_date__lte=today,
        duration_minutes__isnull=False,
    ).aggregate(total=Sum('duration_minutes'))['total'] or 0

    # 上周投入时间
    time_last_week = Activity.objects.filter(
        user=user, status='done',
        end_date__gte=last_week_start, end_date__lt=week_start,
        duration_minutes__isnull=False,
    ).aggregate(total=Sum('duration_minutes'))['total'] or 0

    # 本周消费
    week_expense = float(
        Expense.objects.filter(user=user, paid_at__gte=week_start, paid_at__lte=today)
        .aggregate(s=Sum('amount'))['s'] or 0
    )
    last_week_expense = float(
        Expense.objects.filter(user=user, paid_at__gte=last_week_start, paid_at__lt=week_start)
        .aggregate(s=Sum('amount'))['s'] or 0
    )

    # 消费分类
    cat_labels = dict(Expense.CATEGORY_CHOICES)
    expense_by_category = {
        cat_labels.get(cat, cat): float(amount)
        for cat, amount in Expense.objects.filter(
            user=user, paid_at__gte=week_start, paid_at__lte=today
        ).values('category').annotate(s=Sum('amount')).values_list('category', 's')
    }

    # 习惯连续打卡
    streaks = compute_habit_streaks(user, today)
    habit_streaks = [{'name': s['name'], 'streak': s['streak']} for s in streaks[:3]]

    # 明日安排
    tomorrow = today + timedelta(days=1)
    tomorrow_activities = list(
        Activity.objects.filter(user=user, start_date=tomorrow)
        .exclude(status='cancelled').order_by('name')
        .values_list('name', flat=True)[:5]
    )

    # 进行中活动
    in_progress = list(
        Activity.objects.filter(user=user, status='in_progress')
        .order_by('-start_date').values_list('name', flat=True)[:10]
    )

    return {
        'date': today.isoformat(),
        'done_this_week': done_this_week,
        'time_this_week_minutes': time_this_week,
        'time_last_week_minutes': time_last_week,
        'week_expense': week_expense,
        'last_week_expense': last_week_expense,
        'expense_by_category': expense_by_category,
        'habit_streaks': habit_streaks,
        'tomorrow_activities': tomorrow_activities,
        'in_progress': in_progress,
    }


def build_insight_prompt(data, memory_text):
    """构造 AI 洞察提示词"""
    return f"""请根据用户近期的行为数据和个人记忆，生成 1-2 条今日个性化洞察。

要求：
1. 严格输出 JSON 数组（不要 Markdown 代码块包裹）：
   [{{"text": "...", "icon": "goal|expense|habit|plan|calendar|alert", "action": {{"label": "≤4字", "url": "..."}} 或 null}}]
2. 每条 text ≤ 50 字，自然亲切，必须基于提供的数据，不得虚构
3. 结合用户记忆给出个性化建议（如目标推进、习惯保持），不要复述数据本身
4. action.url 只能从以下页面选择：{', '.join(sorted(URL_WHITELIST))}
5. 没有值得说的洞察时输出空数组 []

数据：
{json.dumps(data, ensure_ascii=False, indent=2)}

{memory_text}"""


def parse_insights(raw):
    """解析 AI 返回的 JSON，校验格式，返回合法洞察列表（最多 2 条）"""
    if not raw:
        return []

    text = raw.strip()
    # 剥掉可能的 ```json 围栏
    if text.startswith('```'):
        lines = text.split('\n')
        lines = [l for l in lines if not l.strip().startswith('```')]
        text = '\n'.join(lines)

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []

    if not isinstance(items, list):
        return []

    valid = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text_val = item.get('text', '').strip()
        if not text_val or len(text_val) > 100:
            continue
        icon = item.get('icon', 'plan')
        if icon not in ICON_WHITELIST:
            icon = 'plan'
        action = item.get('action')
        if isinstance(action, dict) and action.get('url'):
            url = action['url']
            if url not in URL_WHITELIST:
                action = None
            else:
                action = {'label': str(action.get('label', '查看'))[:4], 'url': url}
        else:
            action = None
        valid.append({'text': text_val, 'icon': icon, 'action': action})
        if len(valid) >= 2:
            break

    return valid


def build_fallback_insights(data):
    """AI 失败时的规则模板洞察"""
    insights = []

    time_this = data.get('time_this_week_minutes', 0)
    time_last = data.get('time_last_week_minutes', 0)
    done_count = len(data.get('done_this_week', []))
    if done_count > 0:
        hours = time_this / 60 if time_this else 0
        text = f'本周已完成 {done_count} 项'
        if hours > 0:
            text += f'，投入约 {hours:.0f} 小时'
        if time_last > 0 and time_this > time_last:
            text += '，比上周更专注了'
        insights.append({'text': text, 'icon': 'plan', 'action': None})

    streaks = data.get('habit_streaks', [])
    if streaks and streaks[0]['streak'] >= 3:
        s = streaks[0]
        insights.append({
            'text': f'「{s["name"]}」已连续 {s["streak"]} 天，坚持就是胜利',
            'icon': 'habit',
            'action': {'label': '习惯', 'url': '/activities/recurring/'},
        })

    return insights[:2]


class Command(BaseCommand):
    help = '为每个活跃用户生成每日个性化洞察（AI 优先，失败降级为规则模板，幂等可重跑）'

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
                data = collect_insight_data(user, today)
            except Exception as e:
                logger.error(f'每日洞察数据聚合失败 user={user.id}: {e}')
                failed += 1
                continue

            existing = DailyInsight.objects.filter(
                user=user, insight_date=today
            ).first()

            if dry_run:
                action = '跳过（当日已有洞察）' if existing and existing.status != 'pending' \
                    else ('更新' if existing else '新建')
                self.stdout.write(
                    f'[dry-run] {user.username}: '
                    f'完成 {len(data["done_this_week"])} / '
                    f'投入 {data["time_this_week_minutes"]}min / '
                    f'消费 ¥{data["week_expense"]:.0f} / '
                    f'习惯 {len(data["habit_streaks"])} → {action}'
                )
                continue

            # 幂等：当日已存在 ready/fallback 记录则跳过
            if existing and existing.status != 'pending':
                skipped += 1
                continue

            insights, status = self._generate(user, data)
            if existing:
                existing.insights = insights
                existing.status = status
                existing.generated_at = timezone.now()
                existing.save()
                updated += 1
            else:
                DailyInsight.objects.create(
                    user=user,
                    insight_date=today,
                    insights=insights,
                    status=status,
                    generated_at=timezone.now(),
                )
                created += 1

            # 清除建议缓存，确保用户下次访问可见
            cache.delete(f'suggestions_{user.id}')

        msg = f'完成：新建 {created}，更新 {updated}，跳过 {skipped}'
        if failed:
            msg += f'，失败 {failed}'
        if dry_run:
            self.stdout.write('[dry-run] 未写入数据库')
        self.stdout.write(msg)

    def _generate(self, user, data):
        """AI 生成，失败降级为规则模板。返回 (insights, status)"""
        from memory.services import retrieve_memories, format_memory_for_injection

        memories = retrieve_memories(user, query='', limit=15)
        memory_text = format_memory_for_injection(memories) if memories else ''

        try:
            prompt = build_insight_prompt(data, memory_text)
            result = ai_round_trip(prompt, timeout=90)
        except Exception as e:
            logger.warning(f'每日洞察 AI 生成失败: {e}')
            result = None

        if result:
            insights = parse_insights(result)
            if insights:
                # 补充 key 和 source 字段
                for i, item in enumerate(insights):
                    item['key'] = f'ai:{data["date"]}:{i}'
                    item['source'] = 'ai'
                return insights, 'ready'

        # 降级
        fallback = build_fallback_insights(data)
        for i, item in enumerate(fallback):
            item['key'] = f'ai:{data["date"]}:{i}'
            item['source'] = 'ai'
        return fallback, 'fallback'
