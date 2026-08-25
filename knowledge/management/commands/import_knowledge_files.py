import os
from django.core.management.base import BaseCommand
from django.conf import settings
from knowledge.models import Article
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = '导入 media/knowledge/ 下的 Markdown 文件为知识库文章'

    def handle(self, *args, **options):
        knowledge_dir = settings.MEDIA_ROOT / 'knowledge'
        if not knowledge_dir.exists():
            self.stdout.write('media/knowledge/ 目录不存在')
            return
        
        User = get_user_model()
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            self.stdout.write('没有超级用户，请先创建')
            return
        
        for f in knowledge_dir.glob('*.md'):
            title = f.stem
            if Article.objects.filter(title=title).exists():
                self.stdout.write(f'跳过已存在的：{title}')
                continue
            content = f.read_text(encoding='utf-8')
            Article.objects.create(user=user, title=title, content=content)
            self.stdout.write(f'已导入：{title}')
