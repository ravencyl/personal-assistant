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
    """组装每日早报 payload：今日日程 + 未完成 + 引导（与 chips 同口径）"""
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

    parts = []
    if today_count == 1:
        parts.append(f'今天有「{today_acts.first().name}」')
    elif today_count > 1:
        names = list(today_acts.values_list('name', flat=True)[:3])
        suffix = '等' if today_count > 3 else ''
        parts.append(f'今天有 {today_count} 个活动：'
                     + '、'.join(f'「{n}」' for n in names) + suffix)
    if undone:
        parts.append(f'{undone} 个活动还没完成')
    if not parts:
        parts.append('今天暂无日程安排')

    return {
        'title': f'{settings.SITE_NAME} · 今日早报',
        'body': '；'.join(parts) + '。点开看看今天怎么安排',
        'url': '/',
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
