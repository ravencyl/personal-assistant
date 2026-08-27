from django.contrib import admin
from .models import Memory


@admin.register(Memory)
class MemoryAdmin(admin.ModelAdmin):
    list_display = ('content', 'user', 'category', 'importance', 'access_count', 'created_at')
    list_filter = ('category', 'importance')
    search_fields = ('content',)
    raw_id_fields = ('user', 'source_message')
