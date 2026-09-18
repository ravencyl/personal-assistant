"""Web Push（VAPID）推送能力：服务端组装内容 → 端到端加密 → 厂商投递

为什么是 VAPID 标准协议而不是第三方推送服务：不引入任何外部依赖账号
（FCM 控制台/证书），pywebpush 一个包搞定签名与加密；endpoint 是浏览器
厂商（FCM/APNs）生成的，服务器只负责「用 VAPID 私钥签名 + 加密后投递」。

内容组装复用 core.chips 的同一套查询口径（今日日程/未完成活动）——
早报推送和开场 chips 讲的是同一件事，口径分裂会让用户觉得数据不可信。

发送是出站 HTTPS（到厂商服务器），生产 nginx/gunicorn 无需任何入站改动；
发送失败只 logger.warning，绝不影响调用方（cron / 视图）的主流程。
"""
import json
import logging

from django.conf import settings
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)


def vapid_ready():
    """VAPID 密钥是否已配置（缺任一就把推送视为不可用，前端入口隐藏）"""
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def build_daily_payload(user):
    """组装每日早报 payload：今日日程 + 未完成 + 冲突提示 + 引导（与 chips 同口径）"""
    from django.utils import timezone

    from activities.models import Activity
    from core.utils import visible_qs

    today = timezone.localdate()
    today_acts = (visible_qs(Activity, user)
                  .filter(start_date=today).exclude(status='cancelled'))
    today_count = today_acts.count()

    undone = (visible_qs(Activity, user).filter(status='in_progress').count()
              + visible_qs(Activity, user)
              .filter(status='planned', start_date__lt=today).count())

    # 冲突检测：今天有多个带时间的活动时提示
    timed_today = [a for a in today_acts if a.start_time]
    conflict_count = 0
    if len(timed_today) >= 2:
        for i, a in enumerate(timed_today):
            for b in timed_today[i+1:]:
                a_end = a.end_time or a.start_time
                b_end = b.end_time or b.start_time
                if a.start_time < b_end and b.start_time < a_end:
                    conflict_count += 1

    parts = []
    if today_count == 1:
        parts.append(f'今天有「{today_acts.first().name}」')
    elif today_count > 1:
        names = list(today_acts.values_list('name', flat=True)[:3])
        suffix = '等' if today_count > 3 else ''
        parts.append(f'今天有 {today_count} 个活动：'
                     + '、'.join(f'「{n}」' for n in names) + suffix)
    if conflict_count:
        parts.append(f'{conflict_count} 对活动时间冲突')
    if undone:
        parts.append(f'{undone} 个活动还没完成')
    if not parts:
        parts.append('今天暂无日程安排')

    return {
        'title': f'{settings.SITE_NAME} · 今日早报',
        'body': '；'.join(parts) + '。点开看看今天怎么安排',
        'url': '/',
    }


def build_task_reminder_payload(user):
    """任务提醒：只聚焦未完成的事（进行中 + 过期未启动），无未完成也推一条安心报"""
    from django.utils import timezone

    from activities.models import Activity
    from core.utils import visible_qs

    today = timezone.localdate()
    undone_names = list(
        visible_qs(Activity, user).filter(status='in_progress')
        .values_list('name', flat=True)[:3])
    overdue_qs = (visible_qs(Activity, user)
                  .filter(status='planned', start_date__lt=today))
    overdue_count = overdue_qs.count()
    undone_names += list(overdue_qs.values_list('name', flat=True)
                         [:3 - len(undone_names)])

    if not undone_names and not overdue_count:
        body = '当前没有未完成的任务，安排得井井有条'
    else:
        total = (visible_qs(Activity, user).filter(status='in_progress').count()
                 + overdue_count)
        body = f'{total} 个任务待处理：' + '、'.join(f'「{n}」' for n in undone_names)

    return {
        'title': f'{settings.SITE_NAME} · 任务提醒',
        'body': body + '。点开处理',
        # 深链直达「未完成」筛选视图（in_progress + planned 多值，见 filter_activities）
        'url': '/activities/?status=in_progress,planned',
    }


def build_ai_summary_payload(user):
    """AI 任务总结：把今日/未完成活动交给 AI 生成一句人话总结

    AI 失败（无配置/超时/异常）降级为规则模板（与 chips 同口径），
    绝不因 AI 挂掉而丢推送 —— 通知的价值在「每天准点有」，不在内容多聪明。"""
    from django.utils import timezone

    from activities.models import Activity
    from core.ai import ai_round_trip
    from core.utils import visible_qs

    today = timezone.localdate()
    today_acts = (visible_qs(Activity, user)
                  .filter(start_date=today).exclude(status='cancelled'))
    undone_acts = (visible_qs(Activity, user).filter(status='in_progress')
                   | visible_qs(Activity, user)
                   .filter(status='planned', start_date__lt=today))

    if not today_acts.exists() and not undone_acts.exists():
        lines = ['今天暂无日程安排']
    else:
        lines = (['今日日程：']
                 + [f'- {a.name}（{a.get_status_display()}）'
                    for a in today_acts[:10]]
                 + ['未完成：']
                 + [f'- {a.name}（{a.get_status_display()}）'
                    for a in undone_acts[:10]])

    body = None
    if lines != ['今天暂无日程安排']:
        try:
            reply = ai_round_trip(
                '以下是用户的日程数据，请用不超过 60 字的中文写一段早报式总结，'
                '口语化、突出最该关心的一两件事，不要列表不要寒暄：\n'
                + '\n'.join(lines),
                timeout=60, purpose='general')
            if reply:
                body = reply.strip().splitlines()[0][:120]
        except Exception as exc:
            logger.warning('AI 总结推送降级为规则模板: %s', exc)

    if not body:
        # 规则兜底：与 daily_brief 同口径
        fallback = build_daily_payload(user)
        body = fallback['body']

    return {
        'title': f'{settings.SITE_NAME} · AI 总结',
        'body': body,
        'url': '/',
    }


def build_payload(user, push_type):
    """按计划类型分发内容组装（新类型 = 加分支 + PushSchedule.TYPE_CHOICES 加项 ）"""
    if push_type == 'task_reminder':
        return build_task_reminder_payload(user)
    if push_type == 'ai_summary':
        return build_ai_summary_payload(user)
    if push_type == 'weekly_review':
        return build_weekly_review_payload(user)
    if push_type == 'activity_reminder':
        return build_activity_reminder_payload(user)
    if push_type == 'weekly_report':
        return build_report_push_payload(user, 'weekly')
    if push_type == 'monthly_report':
        return build_report_push_payload(user, 'monthly')
    return build_daily_payload(user)


def build_report_push_payload(user, report_type):
    """周报/月报推送：到期自动生成报告存入知识库，推送摘要 + 深链文章

    幂等分两层：send_scheduled_push 的 last_sent_date 管「同一天只扫一次」，
    这里靠「本周期已有 report-weekly/monthly 标签文章则复用」管「同周期只
    生成一份」（与 core.views 报告页同一判据）。生成走 AI（降级规则模板），
    在 cron 里阻塞几十秒可接受。
    """
    import datetime as dt

    from django.db.models import Sum
    from django.urls import reverse
    from django.utils import timezone

    from activities.models import Activity, Expense
    from core.report_generator import generate_report, save_report_to_knowledge
    from core.utils import visible_qs, week_monday
    from knowledge.models import Article

    today = timezone.localdate()
    is_weekly = report_type == 'weekly'
    label = '周报' if is_weekly else '月报'
    tag = 'report-weekly' if is_weekly else 'report-monthly'

    if is_weekly:
        period_start = week_monday(today)
        title = (f'周报 · {today.year}年第{period_start.isocalendar()[1]}周 '
                 f'({period_start:%m.%d}-{today:%m.%d})')
    else:
        period_start = today.replace(day=1)
        title = f'月报 · {today:%Y年%m月}'
    period_start_aware = timezone.make_aware(
        dt.datetime.combine(period_start, dt.time.min))

    article = Article.objects.filter(
        user=user, tags__name=tag, created_at__gte=period_start_aware,
    ).first()
    if article is None:
        markdown, _data = generate_report(user, report_type, period_start, today)
        article = save_report_to_knowledge(user, report_type, title, markdown)

    # 摘要统计：花费合计属「个人指标」口径，按 user 过滤（不混他人数据）
    acts = visible_qs(Activity, user).filter(
        start_date__gte=period_start, start_date__lte=today)
    done = acts.filter(status='done').count()
    total = acts.count()
    expense = (Expense.objects.filter(
        user=user, paid_at__gte=period_start, paid_at__lte=today,
    ).aggregate(s=Sum('amount'))['s'] or 0)

    span = '本周' if is_weekly else '本月'
    return {
        'title': f'{settings.SITE_NAME} · {label}',
        'body': f'{span}完成 {done}/{total} 个活动，花费 ¥{expense:.0f}，{label}已生成',
        'url': reverse('knowledge:article_detail', args=[article.slug]),
    }


def build_weekly_review_payload(user):
    """每周回顾推送：对话式引导，深链到聊天页自动发起回顾对话"""
    from django.utils import timezone
    from activities.models import Activity
    from core.utils import visible_qs, week_monday

    today = timezone.localdate()
    week_start = week_monday(today)

    # 本周统计
    week_activities = visible_qs(Activity, user).filter(
        start_date__gte=week_start, start_date__lte=today
    )
    completed = week_activities.filter(status='done').count()
    total = week_activities.count()

    body = f'本周完成了 {completed}/{total} 个活动，来聊聊感受？'

    return {
        'title': f'{settings.SITE_NAME} · 每周回顾',
        'body': body,
        'url': '/chat/?ask=weekly_review',
    }


def build_activity_reminder_payload(user):
    """活动前提醒：扫描未来 30 分钟内即将开始的活动，深链直达详情

    幂等由 PushSchedule.last_sent_date 保证（同一天同一计划只发一次）；
    没有即将开始的活动时返回 None，调用方跳过发送。"""
    from datetime import timedelta
    from django.utils import timezone

    from activities.models import Activity
    from core.utils import visible_qs

    now = timezone.localtime()
    soon = now + timedelta(minutes=30)
    today = now.date()

    upcoming = (visible_qs(Activity, user)
                .filter(start_date=today, status__in=['planned', 'in_progress'])
                .exclude(start_time=None)
                .filter(start_time__gte=now.time(), start_time__lte=soon.time())
                .order_by('start_time'))

    if not upcoming.exists():
        return None  # 没有即将开始的活动，不调

    act = upcoming.first()
    count = upcoming.count()

    if count == 1:
        body = f'「{act.name}」将在 30 分钟内开始，点开查看详情'
    else:
        names = list(upcoming.values_list('name', flat=True)[:3])
        body = f'{count} 个活动即将开始：' + '、'.join(f'「{n}」' for n in names)

    return {
        'title': f'{settings.SITE_NAME} · 活动提醒',
        'body': body,
        'url': f'/activities/{act.id}/',
    }


def _send_one(sub, payload):
    """向单条订阅发送。返回 'ok' / 'expired' / 'error'"""
    try:
        webpush(
            subscription_info={
                'endpoint': sub.endpoint,
                'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={'sub': settings.VAPID_SUBJECT},
        )
        return 'ok'
    except WebPushException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in (404, 410):
            # 订阅已失效：用户清了浏览器数据 / 取消授权 / endpoint 过期
            logger.info('push subscription expired (%s): %s', status, sub)
            return 'expired'
        logger.warning('push send failed (%s): %s', status, exc)
        return 'error'
    except Exception as exc:
        # 网络层异常（如国内服务器连不上 fcm.googleapis.com 的 ConnectionError）
        # 不是 WebPushException：不接住会炸掉整轮投递——后面的订阅收不到、
        # last_sent_date 落不了库 → 每 5 分钟重发一次（2026-09-17 线上实测踩到）
        logger.warning('push send failed (network): %s', exc)
        return 'error'


def send_push_to_user(user, payload):
    """向用户全部订阅推送。返回 (sent, cleaned) 计数"""
    from .models import PushSubscription

    subs = PushSubscription.objects.filter(user=user)
    sent = cleaned = 0
    for sub in subs:
        result = _send_one(sub, payload)
        if result == 'ok':
            sent += 1
            from django.utils import timezone
            sub.last_sent_at = timezone.now()
            sub.save(update_fields=['last_sent_at'])
        elif result == 'expired':
            cleaned += 1
            sub.delete()
    return sent, cleaned
