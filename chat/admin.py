from django.contrib import admin
from .models import Conversation, Message


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['title', 'user', 'status', 'created_at', 'updated_at']
    list_filter = ['status']
    search_fields = ['title', 'session_id']
    readonly_fields = ['session_id', 'agent_id', 'created_at', 'updated_at']


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'role', 'content_preview', 'created_at']
    list_filter = ['role']
    readonly_fields = ['created_at']

    def content_preview(self, obj):
        return obj.content[:80]
