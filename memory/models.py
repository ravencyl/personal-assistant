from django.db import models
from django.conf import settings

# Memory.content 字段上限——所有截断处统一引用，不要散落 500 魔法数字
MEMORY_CONTENT_MAX_LENGTH = 500


class Memory(models.Model):
    """AI 长期记忆——跨对话持久化的用户信息

    字段口径对齐 ROA PRD（docs/roa-agent-improvement-plan.md）：
    - type 四层记忆里的三层（working 是会话内临时态，不落这张表）；
      procedural（被纠正的行为/方法）与 semantic 分层，是注入权重最高的层
    - status 生命周期：candidate（门控低置信，等冷路径复验）/ active / stale /
      superseded（被新事实取代，保留推理依据）/ archived / expired
    - superseded_by 取代链：改写记忆不原位覆盖，新建新条 + 旧条指向新条
    - sensitivity 敏感闸门：仅用户显式要求记住时才落库
    """
    CATEGORY_CHOICES = [
        ('preference', '偏好'),
        ('fact', '事实'),
        ('goal', '目标'),
        ('relationship', '关系'),
        ('habit', '习惯'),
        ('other', '其他'),
    ]
    TYPE_CHOICES = [
        ('semantic', '语义'),        # 关于用户的稳定事实（默认，存量条目全归此）
        ('procedural', '程序性'),    # 怎么做对事：纠正、方法。注入权重最高
        ('episodic', '情景'),        # 何时发生了什么。只走检索，不进常驻注入
    ]
    STATUS_CHOICES = [
        ('candidate', '待复验'),
        ('active', '生效'),
        ('stale', '陈旧'),
        ('superseded', '已取代'),
        ('archived', '已归档'),
        ('expired', '已过期'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memories',
    )
    content = models.CharField('记忆内容', max_length=MEMORY_CONTENT_MAX_LENGTH)
    # 索引行一句话（≤150 字）：常驻注入的是索引不是正文，超预算时的裁剪单位
    hook = models.CharField('一句话索引', max_length=150, blank=True, default='')
    type = models.CharField(
        '记忆类型', max_length=12, choices=TYPE_CHOICES, default='semantic',
    )
    category = models.CharField(
        '类别', max_length=20, choices=CATEGORY_CHOICES, default='other',
    )
    importance = models.IntegerField('重要度', default=5)
    # 模型写入时的置信估计 [0,1]；空 = 未评估（规则兜底路径固定给高值）
    confidence = models.FloatField('置信度', null=True, blank=True)
    salience = models.CharField(
        '显著度', max_length=8,
        choices=[('high', '高'), ('normal', '普通'), ('low', '低')],
        default='normal',
    )
    sensitivity = models.CharField(
        '敏感级', max_length=10,
        choices=[('normal', '普通'), ('sensitive', '敏感')],
        default='normal',
    )
    status = models.CharField(
        '状态', max_length=12, choices=STATUS_CHOICES, default='active',
    )
    invalid_at = models.DateField('失效时间', null=True, blank=True)
    superseded_by = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='supersedes', verbose_name='被取代为',
    )
    review_flag = models.BooleanField(
        '待复核', default=False,
        help_text='矛盾消解决定为「存疑」时置位，提醒 owner 求证',
    )
    last_verified = models.DateTimeField('最近证实', null=True, blank=True)
    source_message = models.ForeignKey(
        'chat.Message',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='extracted_memories',
        verbose_name='来源消息',
    )
    access_count = models.IntegerField('访问次数', default=0)
    last_accessed = models.DateTimeField('上次访问', null=True, blank=True)
    consolidated = models.BooleanField(
        '已被聚合', default=False,
        help_text='该条记忆已被 consolidate_memories 命令合成为画像，检索时跳过',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-importance', '-updated_at']
        verbose_name = '记忆'
        verbose_name_plural = '记忆'
        indexes = [
            models.Index(fields=['user', '-importance']),
            models.Index(fields=['user', 'category']),
            models.Index(fields=['user', 'status', 'consolidated'],
                         name='mem_user_st_consolidated'),
        ]

    def __str__(self):
        cat_label = dict(self.CATEGORY_CHOICES).get(self.category, self.category)
        return f'({cat_label}) {self.content[:40]}'


class MemoryAudit(models.Model):
    """记忆操作审计——append-only，只增不改不删（ROA PRD §9.5）

    每次写入/合并/取代/修改/忘掉/遗忘批扫/聚合都落一条。硬删除的记忆靠
    memory=SET_NULL 留下审计行（detail 里带内容摘要），回答「它记过什么、
    什么时候没的」；user 也是溯源口径，user 删除时审计随之清理（CASCADE）。
    """
    ACTION_CHOICES = [
        ('write', '写入'),
        ('merge', '合并'),
        ('candidate', '候选'),
        ('edit', '修改'),
        ('supersede', '取代'),
        ('forget', '忘掉'),
        ('archive', '归档'),
        ('expire', '过期'),
        ('purge', '清除'),
        ('consolidate', '聚合'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memory_audits',
    )
    memory = models.ForeignKey(
        Memory, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='audits', verbose_name='关联记忆',
    )
    action = models.CharField('操作', max_length=20, choices=ACTION_CHOICES)
    detail = models.CharField('摘要', max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '记忆审计'
        verbose_name_plural = '记忆审计'

    def __str__(self):
        return f'[{self.action}] {self.detail[:50]}'
