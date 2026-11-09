"""知识库 = 记忆表里的文章分区（2026-10-28 两库数据层合并）

Article/ArticleVersion 实体已并入 memory.Memory（type='document'）与
memory.MemoryVersion，本文件只留 **Document 代理**：

- proxy 到 Memory，不另建表——全站只有一张知识存储；
- DocumentManager 强制 `type='document'` 过滤 + create 默认值，知识库
  侧代码永远看不到记忆事实条；
- slug/tags/版本历史全在 Memory 上，行为不变（QMind 镜像字段 qmind_* 已随
  云端知识库下线由迁移删除，2026-11-09）。

信号注意：proxy 的 sender 与实体类不同（post_save 会不会触发取决于
保存时用的是哪个类）。core/apps.py 对 Memory 和 Document **两个 sender
都要连接**（FTS 索引与跨模块关联），handler 内按 type 判断是否文章。
"""
from django.db import models

from memory.models import Memory, MemoryVersion  # noqa: F401  （re-export 供旧引用）


class DocumentManager(models.Manager):
    """只看文章分区的 manager：get_queryset 恒等过滤 type='document'"""

    def get_queryset(self):
        return super().get_queryset().filter(type='document')

    def create(self, **kwargs):
        kwargs.setdefault('type', 'document')
        return super().create(**kwargs)


class Document(Memory):
    """知识库文章（Memory 的 document 分区代理，无独立表）"""

    objects = DocumentManager()

    class Meta:
        proxy = True
        verbose_name = '知识文章'
        verbose_name_plural = '知识文章'

    def __str__(self):
        return self.title


# 兼容别名：合并前的大量引用（测试、旧视图变量名等）仍写 Article；
# 实体已并入 Memory，这里统一指到代理类，不再单留一个空壳模型
Article = Document
