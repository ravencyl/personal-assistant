from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # 自动发现各 app 的 agent_tools 模块，完成 Agent 工具注册
        from django.utils.module_loading import autodiscover_modules
        autodiscover_modules('agent_tools')

        # 显式导入 core 内的工具模块（文件名不是 agent_tools.py，不会被自动发现）
        import core.reminder_tools  # noqa: F401
        import core.report_tools  # noqa: F401

        # 关联推荐缓存失效信号
        from django.db.models.signals import post_save, post_delete
        from activities.models import Activity
        from knowledge.models import Article
        from notes.models import Note
        from core.cross_link import invalidate_related_cache

        for model in [Activity, Article, Note]:
            post_save.connect(invalidate_related_cache, sender=model)
            post_delete.connect(invalidate_related_cache, sender=model)
