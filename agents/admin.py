from django.contrib import admin
from .models import AgentConfig, EnvironmentConfig, SessionRecord


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


@admin.register(SessionRecord)
class SessionRecordAdmin(admin.ModelAdmin):
    list_display = ['session_id', 'user', 'status', 'total_credits', 'updated_at']
    list_filter = ['status']
    search_fields = ['session_id', 'title']
    readonly_fields = ['session_id', 'created_at', 'updated_at']
