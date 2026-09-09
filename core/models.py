"""core：横切能力层模型

Tag 是全站唯一标签实体（2026-09 起，自建体系替代 django-taggit）——
django-taggit 不支持自定义 Tag 模型（无法加用途字段），且孤儿/脏标签
没有管理入口。scope 区分用途，各业务模型用 M2M 挂接。
"""
from django.db import models


class Tag(models.Model):
    """标签（跨模块统一）：用途由 scope 区分，同名标签可分用途独立存在

    - 预建：admin 里建好常用标签供表单 autocomplete 选择（taggit 做不到）；
      自由输入仍会自动创建（写入走 core.tags.ensure_tags 单一入口）。
    - 停用：is_active=False 后不再出现在可选项，历史对象的挂接保留、
      照常展示与统计（与费用类别同口径，防止「选不到但还在算」）。
    """

    SCOPE_CHOICES = [
        ('activity', '活动'),
        ('expense', '费用'),
        ('note', '备忘'),
        ('knowledge', '知识库'),
    ]

    scope = models.CharField('用途', max_length=20, choices=SCOPE_CHOICES, db_index=True)
    name = models.CharField('标签名', max_length=50)
    sort = models.PositiveIntegerField('排序', default=100)
    is_active = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '标签'
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=['scope', 'name'], name='uniq_tag_scope_name'),
        ]
        ordering = ['scope', 'sort', 'name']
        indexes = [models.Index(fields=['scope', 'is_active'])]

    def __str__(self):
        return f'{self.name}（{self.get_scope_display()}）'
