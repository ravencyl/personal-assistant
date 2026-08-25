"""批量将 start_date 已到但状态仍为 planned 的活动自动改为 in_progress。

用法：
    python manage.py auto_start_activities [--dry-run]

可配合 Celery Beat 每日定时执行：
    CELERY_BEAT_SCHEDULE = {
        'auto-start-activities': {
            'task': 'django.core.management.call_command',
            'schedule': crontab(hour=0, minute=5),
            'args': ('auto_start_activities',),
        },
    }
"""
from django.core.management.base import BaseCommand

from activities.views import auto_start_activities


class Command(BaseCommand):
    help = '将 start_date 已到但状态仍为 planned 的活动自动改为 in_progress'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='仅显示将要变更的活动，不实际执行',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            from datetime import date
            from django.utils import timezone
            from activities.models import Activity

            today = timezone.localdate()
            qs = Activity.objects.filter(
                status='planned',
                start_date__lte=today,
                start_date__isnull=False,
            )
            count = qs.count()
            self.stdout.write(f'[dry-run] 将变更 {count} 个活动：')
            for a in qs:
                self.stdout.write(f'  - {a.name} (start_date={a.start_date})')
            return

        count = auto_start_activities()
        if count:
            self.stdout.write(self.style.SUCCESS(f'已自动变更 {count} 个活动：planned → in_progress'))
        else:
            self.stdout.write('无需变更的活动')
