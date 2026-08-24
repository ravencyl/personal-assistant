from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # 自动发现各 app 的 agent_tools 模块，完成 Agent 工具注册
        from django.utils.module_loading import autodiscover_modules
        autodiscover_modules('agent_tools')
