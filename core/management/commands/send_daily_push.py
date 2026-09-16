"""每日早报推送：遍历全部订阅用户 → 组装当日内容 → Web Push 发送

幂等可重跑：重跑只会重复通知一次（同 payload），不会产生脏数据；
失效订阅（404/410）在发送过程中自动清理（见 core.push.send_push_to_user）。

cron 建议：0 8 * * *（每天早 8 点，服务器时区需与 TIME_ZONE 一致）。
发送失败只 logger.warning 逐订阅降级，绝不因单条失败中断整批。
"""
import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.push import build_daily_payload, send_push_to_user

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '向所有已订阅用户发送每日早报 Web Push'

    def handle(self, *args, **options):
        User = get_user_model()
        user_ids = list(
            User.objects.filter(push_subscriptions__isnull=False)
            .values_list('id', flat=True)
            .distinct()
        )

        total_sent = total_cleaned = 0
        for user in User.objects.filter(id__in=user_ids):
            try:
                payload = build_daily_payload(user)
                sent, cleaned = send_push_to_user(user, payload)
            except Exception:
                # 单用户内容组装失败不影响其他用户
                logger.exception('daily push failed for user %s', user.pk)
                continue
            total_sent += sent
            total_cleaned += cleaned
            logger.info('daily push user=%s sent=%s cleaned=%s',
                        user.pk, sent, cleaned)

        self.stdout.write(
            f'daily push done: users={len(user_ids)} '
            f'sent={total_sent} cleaned={total_cleaned}'
        )
