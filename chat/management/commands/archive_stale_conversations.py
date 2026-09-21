"""自动归档超期未聊天的对话（cron 调用，幂等可重跑）。

口径（用户 2026-09-21）：超过 chat.models.CHAT_HISTORY_DAYS（3）天没有聊天的
对话自动归档。

- 「最后聊天时间」= 最新一条消息的 created_at；从没发过消息的空对话按
  Conversation.created_at 计（深链创建后没人用的垃圾一并清）。
- 归档动作与手工归档（chat.views.archive_conversation）同一套口径：
  置 status='archived'、沉淀记忆摘要、清 turn 状态、取消平台 session。
- 阈值必须取 models 里的 CHAT_HISTORY_DAYS，与对话页的 3 天展示窗口同源。
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Max, Q
from django.utils import timezone

from chat.models import Conversation, CHAT_HISTORY_DAYS


class Command(BaseCommand):
    help = '归档超过 N 天（默认 chat.models.CHAT_HISTORY_DAYS=3）没有聊天记录的对话'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=CHAT_HISTORY_DAYS,
                            help='闲置天数阈值（默认取 CHAT_HISTORY_DAYS，与展示窗口同源）')
        parser.add_argument('--dry-run', action='store_true',
                            help='只列出将要归档的对话，不写库、不碰平台')

    def handle(self, *args, **options):
        days = options['days']
        cutoff = timezone.now() - timedelta(days=days)
        stale = (Conversation.objects.exclude(status='archived')
                 .annotate(last_msg_at=Max('messages__created_at'))
                 .filter(Q(last_msg_at__lt=cutoff) |
                         Q(last_msg_at__isnull=True, created_at__lt=cutoff))
                 .order_by('id'))

        if options['dry_run']:
            for conv in stale:
                self.stdout.write(f'[dry-run] 将归档对话 {conv.id} {conv.title!r}（user={conv.user_id}）')
            self.stdout.write(f'共 {stale.count()} 个对话待归档')
            return

        # 延迟导入：避免 import 开销叠加，也和 archive_conversation 视图保持同款引用
        from agents.services import get_service
        from memory.services import summarize_conversation_for_memory, extract_review_insights

        archived = 0
        for conv in stale:
            conv.status = 'archived'
            conv.save(update_fields=['status', 'updated_at'])
            archived += 1

            # 与手工归档同口径：摘要失败只少一条记忆，不得影响归档本身
            try:
                summarize_conversation_for_memory(conv)
                extract_review_insights(conv)
            except Exception as e:
                self.stderr.write(f'对话 {conv.id} 归档摘要写入记忆失败: {e}')

            # 归档时还有进行中的一轮：一并清掉，否则 turn 状态永远挂着
            if conv.turn_active:
                conv.reset_turn()

            # 平台侧 session 取消，失败不阻断（3 天没聊天的 session 早已结束）
            try:
                get_service().cancel_session(conv.session_id)
            except Exception:
                pass

        self.stdout.write(f'已归档 {archived} 个超过 {days} 天未聊天的对话')
