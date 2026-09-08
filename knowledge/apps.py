from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'knowledge'
    verbose_name = '知识库'

    def ready(self):
        # Article → QMind 异步镜像同步（knowledge/qmind_sync），失败只记日志
        from django.db.models.signals import post_delete, post_save

        from . import models, qmind_sync

        post_save.connect(qmind_sync.article_saved, sender=models.Article)
        post_delete.connect(qmind_sync.article_deleted, sender=models.Article)
