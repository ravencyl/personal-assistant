from django.contrib import admin
from .models import Note


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ['content', 'user', 'pinned', 'updated_at']
    list_filter = ['pinned', 'updated_at']
    search_fields = ['content']
