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


class PushSubscription(models.Model):
    """Web Push 订阅（VAPID）：浏览器厂商推送服务的投递地址 + 端到端加密密钥

    - endpoint 由浏览器厂商生成、全局唯一（同设备重复订阅是同一个地址），
      unique + update_or_create 让重复「开启推送」幂等；
    - p256dh/auth 是服务器向厂商服务加密消息所需的密钥对，缺失即推不了；
    - 发送侧遇到 404/410（用户清了站点数据或订阅过期）自动删记录。"""

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE,
                             related_name='push_subscriptions')
    endpoint = models.URLField('投递地址', max_length=500, unique=True)
    p256dh = models.CharField('加密公钥', max_length=100)
    auth = models.CharField('认证密钥', max_length=100)
    user_agent = models.CharField('设备标识', max_length=200, blank=True, default='')
    created_at = models.DateTimeField('订阅时间', auto_now_add=True)
    last_sent_at = models.DateTimeField('最近推送', null=True, blank=True)

    class Meta:
        verbose_name = '推送订阅'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} · {self.endpoint[:40]}…'


class PushSchedule(models.Model):
    """推送计划：内容类型 × 触发时间，admin 可配，无需改代码/cron 就能换玩法

    - 一条记录 = 一个「内容 × 时间点」；要多次推送就建多条（如 8 点早报 +
      20 点提醒），每天各发一次，互不影响；
    - 触发由 send_scheduled_push 命令（cron 每 5 分钟）扫描，用 last_sent_date
      保证「同一天同一计划只发一次」：cron 间隔任意改都不会重复，
      服务重启错过时间点也会补发一次；
    - 新内容类型 = core.push.build_payload 加一个分支 + 这里的 choices 加一项。"""

    TYPE_CHOICES = [
        ('daily_brief', '今日早报'),
        ('task_reminder', '任务提醒'),
        ('ai_summary', 'AI 任务总结'),
    ]

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE,
                             related_name='push_schedules')
    push_type = models.CharField('内容类型', max_length=20, choices=TYPE_CHOICES)
    time = models.TimeField('触发时间')
    enabled = models.BooleanField('启用', default=True)
    last_sent_date = models.DateField('最近发送日期', null=True, blank=True,
                                      help_text='幂等标记：同一天同一计划只发一次')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '推送计划'
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=['user', 'push_type', 'time'],
                                    name='uniq_push_schedule_user_type_time'),
        ]
        ordering = ['time']

    def __str__(self):
        # str(self.time)[:5] 而非 {self.time:%H:%M}：实例内存里 time 可能仍是
        # 未经字段转换的 str（get_or_create 后直接 print 就会踩），time.strftime 会炸
        return f'{self.user.username} · {self.get_push_type_display()} · {str(self.time)[:5]}'
