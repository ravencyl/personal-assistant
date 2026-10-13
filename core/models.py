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
    # 活动标签的展示底色（#RRGGBB）。创建时留空 → 随机生成一枚中饱和色，
    # 页面用 color-mix 派生浅底/描边（见 custom.css 的 .tag-wash/.tag-chip）；
    # 仅 activity scope 使用，其余 scope 留空。
    color = models.CharField('底色', max_length=7, blank=True, default='',
                             help_text='活动标签的展示底色，留空保存时自动随机生成')
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

    @property
    def rgb_triplet(self):
        """'#34d399' → '52, 211, 153'（模板 rgba(var(--tag-rgb), α) 用）。

        不走 color-mix 派生浅底：iOS 16.2 以下不认 color-mix，整条声明
        会被静默丢弃（手机端卡片不显色的根因之一）；rgba + 三通道变量
        全平台通用。兜底灰只在 color 字段异常（非 6 位 hex）时出现。
        """
        c = self.color.lstrip('#')
        if len(c) != 6:
            return '113, 113, 122'
        return ', '.join(str(int(c[i:i + 2], 16)) for i in (0, 2, 4))

    def save(self, *args, **kwargs):
        # 随机底色只在「落库且仍为空」这一刻生成：admin 改过就保留，改回空再存会重发一枚
        if self.scope == 'activity' and not self.color:
            from .tags import random_tag_color
            self.color = random_tag_color()
        super().save(*args, **kwargs)


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
        ('weekly_review', '每周回顾'),
        ('activity_reminder', '活动前提醒'),
        ('weekly_report', '周报推送'),
        ('monthly_report', '月报推送'),
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
