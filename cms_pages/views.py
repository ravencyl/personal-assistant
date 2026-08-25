from django.shortcuts import render
from django.contrib.auth.decorators import login_required

from chat.models import Conversation
from activities.models import Activity
from core.utils import visible_qs


@login_required
def dashboard(request):
    """首页仪表盘（超级用户可见全站数据）"""
    user = request.user

    # 统计数据
    conversations = visible_qs(Conversation, user)
    activities = visible_qs(Activity, user)

    recent_conversations = conversations[:5]
    recent_activities = activities[:5]
    ongoing_activities = activities.filter(status__in=['planned', 'in_progress'])

    stats = {
        'total_conversations': conversations.count(),
        'total_activities': activities.count(),
        'ongoing_count': ongoing_activities.count(),
    }

    return render(request, 'cms_pages/dashboard.html', {
        'recent_conversations': recent_conversations,
        'recent_activities': recent_activities,
        'stats': stats,
    })
