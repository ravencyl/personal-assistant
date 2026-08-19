from django.contrib import admin
from .models import AgentConfig, EnvironmentConfig


@admin.register(AgentConfig)
class AgentConfigAdmin(admin.ModelAdmin):
    list_display = ['name', 'purpose', 'model', 'version', 'is_active', 'updated_at']
    list_filter = ['purpose', 'is_active', 'model']
    search_fields = ['name', 'description']
    readonly_fields = ['agent_id', 'created_at', 'updated_at']


@admin.register(EnvironmentConfig)
class EnvironmentConfigAdmin(admin.ModelAdmin):
    list_display = ['name', 'env_id', 'is_default', 'updated_at']
    list_filter = ['is_default']
    readonly_fields = ['env_id', 'created_at', 'updated_at']
