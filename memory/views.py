"""记忆管理视图

提供记忆列表（含类别筛选 + 搜索）、编辑、删除功能。
所有查询按用户隔离（超级用户见全部）。
"""
import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from core.utils import visible_qs, get_visible
from .models import Memory

logger = logging.getLogger(__name__)


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
