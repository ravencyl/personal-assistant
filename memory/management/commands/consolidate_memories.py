"""周期性记忆聚合：把同类别碎片合成结构化画像

每周日凌晨由 cron 触发（见 DEPLOY.md）。同一用户、同一 category 下
≥5 条未聚合记忆时，调 AI 合成为一条画像，原始记忆标记 consolidated=True
（不删除，可追溯/回滚）。

容错：AI 调用失败跳过该组，不影响其他组；整条命令失败退出码仍为 0
（cron 不应因单组失败而报警）。
"""
import logging

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from memory.models import Memory

logger = logging.getLogger(__name__)

# 同组至少多少条才触发聚合（太少不值得调 AI）
MIN_GROUP_SIZE = 5

# AI 合成 prompt 模板
CONSOLIDATE_PROMPT = (
    '以下是用户关于「{category}」的 {count} 条零散记忆片段：\n'
    '{items}\n\n'
    '请将它们合成为一条结构化的用户画像描述（不超过 200 字），'
    '保留关键细节、去除重复、合并相似条目。'
    '只输出合成后的文本，不要解释过程。'
)

CATEGORY_LABELS = dict(Memory.CATEGORY_CHOICES)


def _call_ai(prompt: str) -> str | None:
    """调 AI 合成，返回文本或 None（失败时）"""
    try:
        from agents.services import get_service
        service = get_service()
        # 用临时 session，不复用对话
        session = service.create_session(
            agent_id=None,  # 走默认 agent
            system_prompt='你是一个信息整理助手，擅长把零散笔记合成简洁画像。',
        )
        reply = service.send_message_and_wait(session['id'], prompt, timeout=60)
        return (reply or '').strip() or None
    except Exception as e:
        logger.warning(f'AI 聚合调用失败: {e}')
        return None


class Command(BaseCommand):
    help = '聚合同类别记忆碎片为结构化画像（每周 cron 触发）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='只打印将要聚合的组，不实际执行',
        )
        parser.add_argument(
            '--min-size', type=int, default=MIN_GROUP_SIZE,
            help=f'触发聚合的最小条数（默认 {MIN_GROUP_SIZE}）',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        min_size = options['min_size']

        # 找出所有有 ≥min_size 条未聚合记忆的 (user, category) 组
        groups = (
            Memory.objects
            .filter(consolidated=False)
            .values('user_id', 'category')
            .annotate(cnt=Count('id'))
            .filter(cnt__gte=min_size)
        )

        total_groups = groups.count()
        if total_groups == 0:
            self.stdout.write('没有需要聚合的记忆组。')
            return

        self.stdout.write(f'找到 {total_groups} 个待聚合组。')
        success, skipped = 0, 0

        for group in groups:
            user_id = group['user_id']
            category = group['category']
            cat_label = CATEGORY_LABELS.get(category, category)

            memories = list(
                Memory.objects.filter(
                    user_id=user_id, category=category, consolidated=False,
                ).order_by('-importance', '-updated_at')[:20]  # 上限 20 条防 prompt 过长
            )

            if dry_run:
                self.stdout.write(
                    f'  [DRY-RUN] user={user_id} {cat_label}: {len(memories)} 条'
                )
                continue

            # 构造 prompt
            items_text = '\n'.join(f'- {m.content}' for m in memories)
            prompt = CONSOLIDATE_PROMPT.format(
                category=cat_label, count=len(memories), items=items_text,
            )

            # 调 AI
            summary = _call_ai(prompt)
            if not summary:
                logger.warning(f'聚合跳过: user={user_id} {cat_label} (AI 返回空)')
                skipped += 1
                continue

            # 取组内最高 importance
            max_importance = max(m.importance for m in memories)

            # 创建聚合记忆
            consolidated_memory = Memory.objects.create(
                user_id=user_id,
                content=summary[:500],
                category=category,
                importance=max_importance,
            )

            # 标记原始记忆为已聚合
            ids = [m.id for m in memories]
            Memory.objects.filter(id__in=ids).update(consolidated=True)

            logger.info(
                f'聚合完成: user={user_id} {cat_label} '
                f'{len(memories)} 条 → "{summary[:60]}..."'
            )
            self.stdout.write(
                f'  ✓ user={user_id} {cat_label}: {len(memories)} 条 → '
                f'"{summary[:40]}..."'
            )
            success += 1

        self.stdout.write(
            f'\n完成: {success} 组聚合成功, {skipped} 组跳过。'
        )
