from django.db import models
from django.conf import settings


class AgentConfig(models.Model):
    """本地 Agent 配置记录，与 Qoder Cloud Agents 同步"""
    agent_id = models.CharField(max_length=64, unique=True, help_text='Qoder agent_xxx ID')
    name = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True)
    model = models.CharField(max_length=32, default='auto')
    instructions = models.TextField(blank=True)
    system_prompt = models.TextField(blank=True)
    tools = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    version = models.IntegerField(default=1)
    purpose = models.CharField(
        max_length=32,
        choices=[
            ('general', '通用对话'),
            ('knowledge', '知识库问答'),
            ('task', '任务管理'),
            ('content', '内容处理'),
            ('code', '代码相关'),
        ],
        default='general'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Agent 配置'
        verbose_name_plural = 'Agent 配置'

    def __str__(self):
        return f'{self.name} ({self.get_purpose_display()})'


class EnvironmentConfig(models.Model):
    """本地 Environment 配置记录"""
    env_id = models.CharField(max_length=64, unique=True, help_text='Qoder env_xxx ID')
    name = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True)
    config = models.JSONField(default=dict)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', '-updated_at']
        verbose_name = 'Environment 配置'
        verbose_name_plural = 'Environment 配置'

    def __str__(self):
        return f'{self.name}{" (默认)" if self.is_default else ""}'


class SessionRecord(models.Model):
    """Session 使用记录"""
    session_id = models.CharField(max_length=64, unique=True, help_text='Qoder sess_xxx ID')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sessions'
    )
    agent_config = models.ForeignKey(
        AgentConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sessions'
    )
    environment_id = models.CharField(max_length=64)
    status = models.CharField(
        max_length=20,
        choices=[
            ('idle', '空闲'),
            ('processing', '处理中'),
            ('archived', '已归档'),
        ],
        default='idle'
    )
    title = models.CharField(max_length=255, blank=True)
    total_credits = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Session 记录'
        verbose_name_plural = 'Session 记录'

    def __str__(self):
        return f'Session {self.session_id[:12]}... ({self.status})'
