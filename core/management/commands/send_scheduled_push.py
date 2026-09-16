"""推送计划扫描：按 admin 里配置的 PushSchedule 逐条触发（内容 × 时间点）

cron 建议：每 5 分钟一次。触发条件 = 计划时间已到 且 last_sent_date 不是今天
→ 幂等（同一天同一计划只发一次，cron 间隔随便改），服务重启错过时间点会
在下一次扫描补发一次。

「同一天只发一次」的边界：计划 8:00、cron 8:04 发出后 last_sent_date=今天，
当天后续扫描全部跳过；次日凌晨的扫描因 time > now 不会误触发。

内容组装失败（含 AI 降级后的规则模板）只 logger.exception 跳过该计划，
绝不中断整批；发送侧失败降级见 core.push.send_push_to_user。
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import PushSchedule
from core.push import build_payload, send_push_to_user

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '扫描推送计划（PushSchedule），到期未发的逐条触发 Web Push'

    def handle(self, *args, **options):
        now = timezone.localtime()
        today = now.date()

        due = (PushSchedule.objects.filter(enabled=True, time__lte=now.time())
               .exclude(last_sent_date=today)
               .select_related('user'))

        sent_total = 0
        for sched in due:
            try:
                payload = build_payload(sched.user, sched.push_type)
                sent, cleaned = send_push_to_user(sched.user, payload)
            except Exception:
                logger.exception('scheduled push failed (schedule=%s)', sched.pk)
                continue
            # 无论是否真发出都标记：无订阅/订阅失效的用户若不标记，会
            # 整天每 5 分钟重试一次（ai_summary 还会白白烧 AI 调用）；
            # 当天漏掉的那次由「订阅成功即推测试通知」兜住体验。
            sched.last_sent_date = today
            sched.save(update_fields=['last_sent_date'])
            sent_total += sent
            logger.info('scheduled push schedule=%s type=%s sent=%s cleaned=%s',
                        sched.pk, sched.push_type, sent, cleaned)

        self.stdout.write(
            f'scheduled push done: due={len(list(due))} sent={sent_total}')
