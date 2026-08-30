"""合并重复参与者（同一用户下姓名仅大小写/首尾空白不同，或用 --map 显式指定）

背景：AI 自动识别曾把「yyx」这类大小写变体建成新联系人，导致参与者列表出现
两条同一个人。合并后所有活动关联归到保留记录上，重复记录删除。

用法：
    python manage.py merge_participants                              # 只报告计划，不写库
    python manage.py merge_participants --apply                      # 执行自动检测到的合并
    python manage.py merge_participants --map "Joe:Joe Yan" --apply  # 额外显式合并别名
    python manage.py merge_participants --user raven.cai --apply

--map 的写法：「别名:保留名」，两边都不区分大小写/忽略首尾空白；保留名不存在时直接报错，
避免拼错名字静默新建联系人。不带 --user 时对每个用户分别应用，不会跨用户误合并。
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
        parser.add_argument('--map', action='append', default=[], metavar='别名:保留名',
                            help='显式合并一组写法不同的参与者，可重复传')

    @transaction.atomic
    def handle(self, *args, **options):
        qs = Participant.objects.select_related('user').order_by('user_id', 'id')
        if options['user']:
            qs = qs.filter(user__username=options['user'])
            if not qs.exists():
                raise CommandError(f'用户 {options["user"]} 名下没有参与者记录')

        by_key = defaultdict(list)
        for p in qs:
            by_key[(p.user_id, p.name.strip().lower())].append(p)

        plan = self._dedupe(self._auto_plan(by_key) + self._explicit_plan(by_key, options['map']))
        if not plan:
            self.stdout.write('没有发现重复参与者，无需处理。')
            return

        apply = options['apply']
        dropped = merged_relations = 0
        for keep, drops in self._group_by_keep(plan):
            self.stdout.write(
                f'\n[{keep.user.username}] 保留「{keep.name}」'
                f'(id={keep.id}，{keep.activities.count()} 个活动)')
            for drop in drops:
                count = drop.activities.count()
                self.stdout.write(f'  合并「{drop.name}」(id={drop.id}，{count} 个活动)')
                dropped += 1
                merged_relations += count
                if apply:
                    self._merge(keep, drop)

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f'\n已合并 {dropped} 条重复参与者，迁移 {merged_relations} 条活动关联。'))
        else:
            self.stdout.write(self.style.WARNING(
                f'\n[dry-run] 计划合并 {dropped} 条、涉及 {merged_relations} 条活动关联；'
                '加 --apply 执行。'))

    def _auto_plan(self, by_key):
        """自动检测：同用户下仅大小写/首尾空白不同的分组，保留最早创建的一条"""
        plan = []
        for group in by_key.values():
            if len(group) < 2:
                continue
            # 保留最早创建的一条：其写法多为用户手工录入的规范形式（如「YYX」）
            keep = min(group, key=lambda p: (p.created_at, p.id))
            plan += [(keep, p) for p in group if p.id != keep.id]
        return plan

    def _group_by_keep(self, plan):
        """按计划顺序把同一个保留名的待合并项归到一起输出"""
        order, grouped = [], {}
        for keep, drop in plan:
            if keep.id not in grouped:
                grouped[keep.id] = (keep, [])
                order.append(keep.id)
            grouped[keep.id][1].append(drop)
        return [grouped[key] for key in order]

    def _explicit_plan(self, by_key, mappings):
        """解析 --map 别名:保留名；目标不存在则报错，绝不静默新建"""
        plan = []
        for item in mappings:
            alias, _, target = item.partition(':')
            alias, target = alias.strip(), target.strip()
            if not alias or not target:
                raise CommandError(f'--map 需要「别名:保留名」格式，收到：{item}')
            matched = 0
            for user_id in sorted({uid for uid, _key in by_key}):
                drops = list(by_key.get((user_id, alias.lower()), []))
                keeps = by_key.get((user_id, target.lower()), [])
                if not drops:
                    continue
                if not keeps:
                    raise CommandError(
                        f'--map「{alias}:{target}」失败：'
                        f'用户 id={user_id} 名下没有名为「{target}」的参与者')
                keep = min(keeps, key=lambda p: (p.created_at, p.id))
                pairs = [(keep, p) for p in drops if p.id != keep.id]
                plan += pairs
                matched += len(pairs)
            if not matched:
                self.stdout.write(f'--map「{alias}:{target}」：没有匹配到别名，已跳过。')
        return plan

    @staticmethod
    def _dedupe(plan):
        """同一 drop 只处理一次（自动检测与显式映射可能重叠）

        互斥映射（A→B 与 B→A）会让已删的一方又当保留名，直接报错而不是把两条都删掉。
        """
        doomed, unique = set(), []
        for keep, drop in plan:
            if drop.id in doomed:
                continue
            if keep.id in doomed:
                raise CommandError(
                    f'合并计划冲突：「{keep.name}」已在前一条计划中被合并，不能再作为保留名')
            doomed.add(drop.id)
            unique.append((keep, drop))
        return unique

    def _merge(self, keep, drop):
        """把 drop 的活动关联迁到 keep，回填备注，删除 drop"""
        for activity in list(drop.activities.all()):
            activity.participants.add(keep)
            activity.participants.remove(drop)
        if not keep.note and drop.note:
            keep.note = drop.note
            keep.save(update_fields=['note'])
        drop.delete()
