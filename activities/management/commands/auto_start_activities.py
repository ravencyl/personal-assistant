"""批量将 start_date 已到但状态仍为 planned 的活动自动改为 in_progress。

与页面访问（activity_list / activity_detail）共用 services.start_due_activities，
只多一份定时兜底，避免长期没人开页面时状态不生效。

用法：
    python manage.py auto_start_activities [--dry-run]
"""
from django.core.management.base import BaseCommand

from activities.services import start_due_activities


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
        changed = start_due_activities(dry_run=dry_run)

        if dry_run:
            self.stdout.write(f'[dry-run] 将变更 {len(changed)} 个活动：')
            for a in changed:
                self.stdout.write(f'  - {a.name} (start_date={a.start_date})')
            return

        if changed:
            self.stdout.write(self.style.SUCCESS(
                f'已自动变更 {len(changed)} 个活动：planned → in_progress'))
        else:
            self.stdout.write('无需变更的活动')
