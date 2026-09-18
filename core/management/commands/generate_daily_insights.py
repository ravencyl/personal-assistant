"""
每日行为模式洞察：分析所有活跃用户的行为模式并存为记忆

cron 每日执行一次，幂等（覆盖旧的模式记忆）。
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '为所有活跃用户生成行为模式洞察并存为记忆'

    def handle(self, *args, **options):
        User = get_user_model()
        from core.patterns import save_pattern_insights

        users = User.objects.filter(is_active=True)
        total_saved = 0

        for user in users:
            try:
                saved = save_pattern_insights(user)
                total_saved += len(saved)
                if saved:
                    self.stdout.write(
                        f'  {user.username}: 保存 {len(saved)} 条洞察'
                    )
            except Exception as exc:
                logger.warning('用户 %s 行为模式分析失败: %s', user.username, exc)

        self.stdout.write(
            self.style.SUCCESS(f'完成：共保存 {total_saved} 条行为模式洞察')
        )
