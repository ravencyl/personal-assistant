from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone

from chat.models import Conversation
from tasks.models import Task
from content.models import Bookmark


@login_required
def dashboard(request):
    """首页仪表盘"""
    user = request.user
    now = timezone.now()

    # 统计数据
    recent_conversations = Conversation.objects.filter(user=user)[:5]
    pending_tasks = Task.objects.filter(user=user, status__in=['pending', 'in_progress'])[:5]
    overdue_tasks = Task.objects.filter(
        user=user,
        status__in=['pending', 'in_progress'],
        due_date__isnull=False,
        due_date__lt=now,
    )
    recent_bookmarks = Bookmark.objects.filter(user=user)[:5]

    stats = {
        'total_conversations': Conversation.objects.filter(user=user).count(),
        'total_tasks': Task.objects.filter(user=user, status__in=['pending', 'in_progress']).count(),
        'overdue_count': overdue_tasks.count(),
        'total_bookmarks': Bookmark.objects.filter(user=user).count(),
    }

    return render(request, 'cms_pages/dashboard.html', {
        'recent_conversations': recent_conversations,
        'pending_tasks': pending_tasks,
        'recent_bookmarks': recent_bookmarks,
        'stats': stats,
    })
