from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'knowledge'
    verbose_name = '知识库'

    # 2026-11-09：QMind 云端镜像同步按用户决定整体下线（本模块 ready() 曾在这里
    # 给 Document/Memory 两个 sender 连 post_save/post_delete）。文章的 FTS 索引
    # 与跨模块关联同步仍在 core/apps.py 里连接，与本模块无关。
