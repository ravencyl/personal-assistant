"""每日执行：根据 RecurringActivity 规则生成未来 7 天的 Activity 实例"""
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from activities.models import Activity, RecurringActivity

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '根据循环活动规则生成未来 7 天的活动实例（幂等）'

    def handle(self, *args, **options):
        today = timezone.localdate()
        horizon = today + timedelta(days=7)
        total_created = 0
        
        for pattern in RecurringActivity.objects.filter(is_active=True):
            created = self._generate_for_pattern(pattern, today, horizon)
            total_created += created
        
        if total_created:
            logger.info(f'循环活动生成: 共创建 {total_created} 个实例')
        self.stdout.write(f'完成，共生成 {total_created} 个活动实例')

    def _generate_for_pattern(self, pattern, start, end):
        created = 0
        current = start
        
        # 如果已有上次生成记录，从上次生成日期的下一天开始
        if pattern.last_generated_date:
            current = pattern.last_generated_date + timedelta(days=1)
        
        while current <= end:
            should_create = False
            
            if pattern.frequency == 'daily':
                should_create = True
            elif pattern.frequency == 'weekly':
                if pattern.day_of_week is not None and current.weekday() == pattern.day_of_week:
                    should_create = True
            elif pattern.frequency == 'monthly':
                if pattern.day_of_month is not None and current.day == pattern.day_of_month:
                    should_create = True
            
            if should_create:
                # 检查是否已存在（幂等）
                exists = Activity.objects.filter(
                    user=pattern.user,
                    recurring_source=pattern,
                    start_date=current,
                ).exists()
                
                if not exists:
                    Activity.objects.create(
                        user=pattern.user,
                        name=pattern.name,
                        start_date=current,
                        end_date=current,
                        status='planned',
                        recurring_source=pattern,
                    )
                    created += 1
            
            current += timedelta(days=1)
        
        # 更新上次生成日期
        pattern.last_generated_date = end
        pattern.save(update_fields=['last_generated_date', 'updated_at'])
        
        return created
