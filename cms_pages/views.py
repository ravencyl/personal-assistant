from django.shortcuts import render
from django.contrib.auth.decorators import login_required

from chat.models import Conversation
from activities.models import Activity
from content.models import Bookmark


@login_required
def dashboard(request):
    """首页仪表盘"""
    user = request.user

    # 统计数据
    recent_conversations = Conversation.objects.filter(user=user)[:5]
    recent_activities = Activity.objects.filter(user=user)[:5]
    ongoing_activities = Activity.objects.filter(
        user=user,
        status__in=['planned', 'in_progress'],
    )
    recent_bookmarks = Bookmark.objects.filter(user=user)[:5]

    stats = {
        'total_conversations': Conversation.objects.filter(user=user).count(),
        'total_activities': Activity.objects.filter(user=user).count(),
        'ongoing_count': ongoing_activities.count(),
        'total_bookmarks': Bookmark.objects.filter(user=user).count(),
    }

    return render(request, 'cms_pages/dashboard.html', {
        'recent_conversations': recent_conversations,
        'recent_activities': recent_activities,
        'recent_bookmarks': recent_bookmarks,
        'stats': stats,
    })
