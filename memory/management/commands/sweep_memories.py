"""遗忘批扫（ROA PRD §5.5）：没有遗忘机制的记忆系统必然被自己拖垮

每周由 cron 触发（见 DEPLOY.md）。四条规则互不重叠、逐条独立执行：

- episodic 超过 TTL（默认 180 天）→ expired（情景是流水账，过期是常态不是损失）
- superseded 超过 180 天 → 硬删除（取代链已保留 180 天，足够回溯；审计行仍在）
- 保留分 < θ_low 且从未被召回且年龄 > 90 天 → archived（低价值旧条让位）
- 保留分 < θ_high → salience 降为 low（移出 procedural 全文注入，仅走检索）

保留分 = w1·confidence + w2·salience + w3·recency(last_verified)
        + w4·normalized(access_count) − w5·staleness
全部参数走环境变量覆盖（禁止硬编码锁死），调参依据是评测集（Phase C），不是直觉。
"""
import logging
import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from memory.models import Memory
from memory.services import log_audit

logger = logging.getLogger(__name__)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, '') or default)
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, '') or default)
    except ValueError:
        return default


# 权重与阈值（PRD §5.5 默认值）。个人助手场景没有足够数据拟合权重，
# 这组是 PRD 给的先验，跑一个月看 archived/purge 数量再调
WEIGHTS = {
    'confidence': _env_float('MEM_W_CONFIDENCE', 0.30),
    'salience': _env_float('MEM_W_SALIENCE', 0.25),
    'recency': _env_float('MEM_W_RECENCY', 0.20),
    'recall': _env_float('MEM_W_RECALL', 0.15),
    'staleness': _env_float('MEM_W_STALENESS', 0.10),
}
THETA_LOW = _env_float('MEM_THETA_LOW', 0.35)
THETA_HIGH = _env_float('MEM_THETA_HIGH', 0.50)
EPISODIC_TTL_DAYS = _env_int('MEM_EPISODIC_TTL_DAYS', 180)
SUPERSEDED_PURGE_DAYS = _env_int('MEM_SUPERSEDED_PURGE_DAYS', 180)
NEVER_RECALLED_AGE_DAYS = _env_int('MEM_NEVER_RECALLED_AGE_DAYS', 90)

SALIENCE_SCORE = {'high': 1.0, 'normal': 0.6, 'low': 0.2}


def retention_score(memory, now):
    """单条记忆的保留分 [约 -0.1, 1.1]。

    confidence 为空（存量旧条/规则路径没存）按 importance/10 折算——
    同一信息源，不能因为 Phase A 之前入库就当 0 分处理。
    """
    confidence = (memory.confidence if memory.confidence is not None
                  else memory.importance / 10)
    salience = SALIENCE_SCORE.get(memory.salience, 0.6)
    anchor = memory.last_verified or memory.updated_at
    recency = 0.5 ** (max(0, (now - anchor).total_seconds() / 86400) / 30)
    recall = min(memory.access_count / 20, 1.0)
    staleness = min(max(0, (now - memory.updated_at).total_seconds() / 86400) / 180, 1)
    return (WEIGHTS['confidence'] * confidence
            + WEIGHTS['salience'] * salience
            + WEIGHTS['recency'] * recency
            + WEIGHTS['recall'] * recall
            - WEIGHTS['staleness'] * staleness)


class Command(BaseCommand):
    help = '遗忘批扫：TTL 过期 / 取代条清除 / 低分归档与降级（每周 cron 触发）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='只打印将要执行的清理，不实际改动',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()
        stats = {'expired': 0, 'purged': 0, 'archived': 0, 'demoted': 0}

        # 1) episodic TTL → expired
        ttl_cut = now - timedelta(days=EPISODIC_TTL_DAYS)
        expired_qs = (Memory.objects.filter(type='episodic', updated_at__lt=ttl_cut)
                      .exclude(status__in=['expired', 'superseded']))
        for memory in expired_qs:
            if not dry_run:
                memory.status = 'expired'
                memory.save(update_fields=['status', 'updated_at'])
                log_audit(memory.user, 'expire', memory=memory,
                          detail=f'情景 TTL {EPISODIC_TTL_DAYS} 天到: {memory.content[:80]}')
            stats['expired'] += 1

        # 2) superseded 超期 → 硬删除（审计行保留内容摘要）
        purge_cut = now - timedelta(days=SUPERSEDED_PURGE_DAYS)
        purged_qs = (Memory.objects.filter(status='superseded', updated_at__lt=purge_cut)
                     .select_related('superseded_by'))
        for memory in purged_qs:
            if not dry_run:
                log_audit(memory.user, 'purge', memory=None,
                          detail=(f'取代超 {SUPERSEDED_PURGE_DAYS} 天清除: '
                                  f'{memory.content[:80]}'
                                  f'（现行条 #{memory.superseded_by_id}）'))
                memory.delete()
            stats['purged'] += 1

        # 3) 低分从未召回 → archived；4) 低分 → salience 降级
        # 只扫仍在「参与召回」的条目；candidate 等复验流程裁决，不归遗忘管
        candidates = (Memory.objects
                      .filter(status='active')
                      .exclude(sensitivity='sensitive')
                      .exclude(type='document'))  # 文章不参与遗忘（去留只归 owner）
        for memory in candidates:
            age_days = (now - memory.updated_at).total_seconds() / 86400
            score = retention_score(memory, now)
            if score < THETA_LOW and memory.access_count == 0 \
                    and age_days > NEVER_RECALLED_AGE_DAYS:
                if not dry_run:
                    memory.status = 'archived'
                    memory.save(update_fields=['status', 'updated_at'])
                    log_audit(memory.user, 'archive', memory=memory,
                              detail=f'保留分 {score:.2f} < {THETA_LOW} 且 90 天未召回: '
                                     f'{memory.content[:80]}')
                stats['archived'] += 1
            elif score < THETA_HIGH and memory.salience != 'low':
                if not dry_run:
                    memory.salience = 'low'
                    memory.save(update_fields=['salience', 'updated_at'])
                    log_audit(memory.user, 'archive', memory=memory,
                              detail=f'保留分 {score:.2f} < {THETA_HIGH} 降级 salience=low: '
                                     f'{memory.content[:80]}')
                stats['demoted'] += 1

        summary = (f'遗忘批扫完成: episodic 过期 {stats["expired"]}, '
                   f'取代条清除 {stats["purged"]}, 归档 {stats["archived"]}, '
                   f'降级 {stats["demoted"]}')
        if dry_run:
            summary = '[DRY-RUN] ' + summary
        self.stdout.write(summary)
        logger.info(summary)
