"""记忆管理视图

提供记忆列表（含类别筛选 + 搜索）、编辑、删除、目标进度、统计 API。
所有查询按用户隔离（超级用户见全部）。
"""
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from core.utils import visible_qs, get_visible
from .models import Memory

logger = logging.getLogger(__name__)


def _compute_goal_progress(user):
    """计算目标进度：返回 [{content, total, done, percent}, ...]"""
    from django.db.models import Q
    from activities.models import Activity

    goals = visible_qs(Memory, user).filter(category='goal')
    result = []
    for goal in goals:
        keywords = goal.content.split()[:3]
        related = Activity.objects.filter(user=user, archived_at__isnull=True)
        q = Q()
        for kw in keywords:
            q |= Q(name__icontains=kw) | Q(tags__name__icontains=kw)
        if q:
            related = related.filter(q)
        else:
            related = related.none()
        total = related.count()
        done = related.filter(status='done').count()
        pct = int(done / total * 100) if total > 0 else 0
        result.append({
            'content': goal.content,
            'total': total,
            'done': done,
            'percent': pct,
        })
    return result


@login_required
def memory_list(request):
    """记忆列表页（支持类别筛选 + 搜索）"""
    memories = visible_qs(Memory, request.user)

    # 类别筛选
    category = request.GET.get('category', '').strip()
    valid_categories = dict(Memory.CATEGORY_CHOICES).keys()
    if category and category in valid_categories:
        memories = memories.filter(category=category)

    # 搜索
    query = request.GET.get('q', '').strip()
    if query:
        memories = memories.filter(content__icontains=query)

    memories = memories.order_by('-importance', '-updated_at')

    # 目标进度（始终计算，模板用 {% if goals %} 控制显示）
    goals = _compute_goal_progress(request.user)

    # HTMX 请求只返回列表片段
    if request.htmx:
        return render(request, 'memory/_memory_items.html', {
            'memories': memories,
            'query': query,
            'category': category,
        })

    return render(request, 'memory/memory_list.html', {
        'memories': memories,
        'category': category,
        'query': query,
        'categories': Memory.CATEGORY_CHOICES,
        'goals': goals,
    })


@login_required
def memory_edit(request, memory_id):
    """编辑记忆（HTMX 局部渲染）"""
    memory = get_visible(Memory, request.user, id=memory_id)

    if request.method == 'POST':
        # 提交编辑
        content = request.POST.get('content', '').strip()
        category = request.POST.get('category', 'other')
        importance = request.POST.get('importance', 5)

        if content:
            memory.content = content[:500]
        if category in dict(Memory.CATEGORY_CHOICES):
            memory.category = category
        try:
            memory.importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            pass
        memory.save()

        if request.htmx:
            # HTMX 返回列表片段
            memories = visible_qs(Memory, request.user).order_by('-importance', '-updated_at')
            return render(request, 'memory/_memory_items.html', {
                'memories': memories,
                'query': '',
                'category': '',
            })
        return redirect('memory:memory_list')

    # GET 请求返回编辑表单
    return render(request, 'memory/_memory_edit_form.html', {
        'memory': memory,
        'categories': Memory.CATEGORY_CHOICES,
    })


@login_required
@require_POST
def memory_delete(request, memory_id):
    """删除记忆"""
    memory = get_visible(Memory, request.user, id=memory_id)

    memory.delete()

    if request.htmx:
        memories = visible_qs(Memory, request.user).order_by('-importance', '-updated_at')
        return render(request, 'memory/_memory_items.html', {
            'memories': memories,
            'query': '',
            'category': '',
        })

    return redirect('memory:memory_list')


@login_required
def goal_progress_view(request):
    """目标进度 API"""
    return JsonResponse({'goals': _compute_goal_progress(request.user)})


@login_required
def memory_stats_api(request):
    """记忆统计 API：分类数量、月度趋势、总览"""
    from django.db.models import Count
    from django.utils import timezone

    all_memories = visible_qs(Memory, request.user)

    # 分类统计
    cat_stats = list(
        all_memories.values('category')
        .annotate(count=Count('id'))
        .order_by('category')
    )

    # 月度趋势（最近 6 个月，按自然月边界）
    now = timezone.now()
    monthly = []
    for i in range(5, -1, -1):
        # 自然月回溯：月-1，年自动进位
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        month_start = now.replace(year=y, month=m, day=1,
                                  hour=0, minute=0, second=0, microsecond=0)
        if m == 12:
            month_end = month_start.replace(year=y + 1, month=1)
        else:
            month_end = month_start.replace(month=m + 1)
        count = all_memories.filter(
            created_at__gte=month_start, created_at__lt=month_end
        ).count()
        monthly.append({
            'month': month_start.strftime('%Y-%m'),
            'count': count,
        })

    # 总览
    summary = {
        'total': all_memories.count(),
        'this_month': all_memories.filter(
            created_at__gte=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        ).count(),
        'high_importance': all_memories.filter(importance__gte=8).count(),
    }

    return JsonResponse({
        'categories': cat_stats,
        'monthly': monthly,
        'summary': summary,
    })
