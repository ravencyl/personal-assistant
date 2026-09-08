"""把存量知识库文章批量同步到 QMind

首次接入或修复漂移时手动执行；日常增量同步由 post_save 信号负责。
幂等可重跑：内容指纹一致且已有 source_id 的文章自动跳过。

用法：
    ./venv/bin/python manage.py sync_knowledge_to_qmind            # 全量（跳过已同步）
    ./venv/bin/python manage.py sync_knowledge_to_qmind --force    # 强制全部重传
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from knowledge.models import Article
from knowledge.qmind import configured, sync_article
from knowledge.qmind_sync import _sync_article


class Command(BaseCommand):
    help = '把存量知识库文章批量同步到 QMind 云端笔记本'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='忽略指纹，全部重新上传（修复云端漂移时用）')

    def handle(self, *args, **options):
        if not configured():
            self.stderr.write('未配置 QMIND_NOTEBOOK_ID 或 QODER_ACCESS_TOKEN，同步未启用')
            return

        force = options['force']
        # 系统级同步命令，不走 visible_qs（镜像不区分归属，超管口径之外的全部存量）
        qs = Article.objects.all()
        if force:
            Article.objects.update(qmind_sync_hash='', qmind_source_id='')
            qs = Article.objects.all()
        else:
            qs = qs.filter(Q(qmind_sync_hash='') | Q(qmind_source_id=''))

        total = qs.count()
        self.stdout.write(f'待同步 {total} 篇文章')
        ok = fail = 0
        for article in qs.iterator():
            before = (article.qmind_source_id, article.qmind_sync_hash)
            _sync_article(article.pk)  # 复用信号的处理函数：失败只记日志
            article.refresh_from_db()
            after = (article.qmind_source_id, article.qmind_sync_hash)
            if after != before and article.qmind_source_id:
                ok += 1
                self.stdout.write(f'  ✓ 《{article.title}》')
            elif article.qmind_sync_hash == article.sync_hash() and article.qmind_source_id:
                ok += 1
            else:
                fail += 1
                self.stderr.write(f'  ✗ 《{article.title}》同步失败（详见日志）')
        self.stdout.write(self.style.SUCCESS(f'完成：成功 {ok}，失败 {fail}（云端去重跳过的计入成功）'))
