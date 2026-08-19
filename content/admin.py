from django.contrib import admin
from .models import Bookmark, ContentCategory, RSSFeed, FeedItem


@admin.register(Bookmark)
class BookmarkAdmin(admin.ModelAdmin):
    list_display = ['title', 'user', 'url', 'category', 'created_at']
    list_filter = ['category']
    search_fields = ['title', 'url', 'description']


@admin.register(ContentCategory)
class ContentCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(RSSFeed)
class RSSFeedAdmin(admin.ModelAdmin):
    list_display = ['name', 'url', 'category', 'last_fetched', 'auto_fetch']
    list_filter = ['auto_fetch', 'category']


@admin.register(FeedItem)
class FeedItemAdmin(admin.ModelAdmin):
    list_display = ['title', 'feed', 'published_at', 'is_read']
    list_filter = ['is_read', 'feed']
    search_fields = ['title', 'description']
