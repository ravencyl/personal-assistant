from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # 自动发现各 app 的 agent_tools 模块，完成 Agent 工具注册
        from django.utils.module_loading import autodiscover_modules
        autodiscover_modules('agent_tools')

        # 显式导入 core 内的工具模块（文件名不是 agent_tools.py，不会被自动发现）
        import core.report_tools  # noqa: F401

        # 关联推荐缓存失效信号。
        # Memory/Document 都要连（proxy sender 与实体类分离）：handler 内按
        # type 判断——只有 document（文章）参与关联推荐缓存，fact 条跳过。
        from django.db.models.signals import post_save, post_delete
        from activities.models import Activity
        from knowledge.models import Document
        from memory.models import Memory
        from notes.models import Note
        from core.cross_link import invalidate_related_cache

        for model in [Activity, Note, Document, Memory]:
            post_save.connect(invalidate_related_cache, sender=model)
            post_delete.connect(invalidate_related_cache, sender=model)

        # FTS5 全文索引同步（同上：Memory/Document 双 sender，handler 判 type）
        from core.fts import sync_to_fts, remove_from_fts
        for model in [Activity, Note, Document, Memory]:
            post_save.connect(sync_to_fts, sender=model)
            post_delete.connect(remove_from_fts, sender=model)
