"""合并重复参与者（同一用户下姓名仅大小写/首尾空白不同）

背景：AI 自动识别曾把「yyx」这类大小写变体建成新联系人，导致参与者列表出现
两条同一个人。合并后所有活动关联归到保留记录上，重复记录删除。

用法：
    python manage.py merge_participants                 # 只报告计划，不写库
    python manage.py merge_participants --apply         # 执行合并
    python manage.py merge_participants --user raven.cai --apply
"""
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from activities.models import Participant


class Command(BaseCommand):
    help = '合并同一用户下仅大小写/首尾空白不同的重复参与者（默认 dry-run，不写库）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正执行合并与删除；不加该参数只输出计划')
        parser.add_argument('--user', default='',
                            help='只处理指定用户名，默认处理全部用户')

    @transaction.atomic
    def handle(self, *args, **options):
        qs = Participant.objects.select_related('user').order_by('user_id', 'id')
        if options['user']:
            qs = qs.filter(user__username=options['user'])
            if not qs.exists():
                raise CommandError(f'用户 {options["user"]} 名下没有参与者记录')

        groups = defaultdict(list)
        for p in qs:
            groups[(p.user_id, p.name.strip().lower())].append(p)

        dup_groups = [g for g in groups.values() if len(g) > 1]
        if not dup_groups:
            self.stdout.write('没有发现重复参与者，无需处理。')
            return

        apply = options['apply']
        dropped = merged_relations = 0
        for group in dup_groups:
            # 保留最早创建的一条：其写法多为用户手工录入的规范形式（如「YYX」）
            keep = min(group, key=lambda p: (p.created_at, p.id))
            self.stdout.write(
                f'\n[{keep.user.username}] 保留「{keep.name}」'
                f'(id={keep.id}，{keep.activities.count()} 个活动)')
            for p in [x for x in group if x.id != keep.id]:
                count = p.activities.count()
                self.stdout.write(f'  合并「{p.name}」(id={p.id}，{count} 个活动)')
                dropped += 1
                merged_relations += count
                if apply:
                    for activity in list(p.activities.all()):
                        activity.participants.add(keep)
                        activity.participants.remove(p)
                    if not keep.note and p.note:
                        keep.note = p.note
                        keep.save(update_fields=['note'])
                    p.delete()

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f'\n已合并 {dropped} 条重复参与者，迁移 {merged_relations} 条活动关联。'))
        else:
            self.stdout.write(self.style.WARNING(
                f'\n[dry-run] 计划合并 {dropped} 条、涉及 {merged_relations} 条活动关联；'
                '加 --apply 执行。'))
