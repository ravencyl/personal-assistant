from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'knowledge'
    verbose_name = '知识库'

    def ready(self):
        # Memory(document) → QMind 异步镜像同步（knowledge/qmind_sync），失败只记日志。
        # proxy 的 sender 与实体类不同：保存走 Document、迁移/旁路可能走 Memory，
        # 两个 sender 都要连，handler 内按 type 判断（fact 条直接跳过）。
        from django.db.models.signals import post_delete, post_save

        from memory.models import Memory

        from . import qmind_sync, models

        post_save.connect(qmind_sync.article_saved, sender=models.Document)
        post_save.connect(qmind_sync.article_saved, sender=Memory)
        post_delete.connect(qmind_sync.article_deleted, sender=models.Document)
        post_delete.connect(qmind_sync.article_deleted, sender=Memory)
