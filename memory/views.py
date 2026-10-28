"""记忆管理视图

提供记忆列表（含类别筛选 + 搜索 + 分页）、编辑、删除、目标进度、统计 API。
所有查询按用户隔离（超级用户见全部）。
"""
import logging
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from core.utils import visible_qs, get_visible, page_window
from .models import Memory, MemoryAudit, MEMORY_CONTENT_MAX_LENGTH

logger = logging.getLogger(__name__)

# 列表分页大小：与活动列表同量级，长期使用也不会再把页面拉成无限长
MEMORY_PAGE_SIZE = 30


def _list_params(request):
    """列表页与 HTMX 片段共用的筛选/分页参数。

    GET 从查询串取；POST（编辑保存）从表单的 filter_* 隐藏字段取——
    不能叫 q/category：编辑表单自己就有 name="category"（记忆类别），
    同名会撞车（UI 走查 major：保存后片段丢筛选）。
    """
    if request.method == 'POST':
        query = request.POST.get('filter_q', '').strip()
        category = request.POST.get('filter_category', '').strip()
        raw_page = request.POST.get('filter_page', '1')
    else:
        query = request.GET.get('q', '').strip()
        category = request.GET.get('category', '').strip()
        raw_page = request.GET.get('page', '1')
    valid_categories = dict(Memory.CATEGORY_CHOICES).keys()
    if category not in valid_categories:
        category = ''
    try:
        page = max(1, int(raw_page))
    except (TypeError, ValueError):
        page = 1
    return query, category, page


def _filter_qs(query, category, page=None):
    """翻页链接 / 编辑携带用的查询串：空筛选不序列化，page 显式传入才带"""
    parts = []
    if query:
        parts.append(('q', query))
    if category:
        parts.append(('category', category))
    if page is not None:
        parts.append(('page', page))
    return urlencode(parts)


def _memory_list_context(request):
    """_memory_items.html 片段的统一口径（整页首屏 include 也走这里）。

    筛选、翻页、分页控件、编辑触发参数全部由同一个函数产出，
    保证「筛选 / 保存 / 取消」三条 swap 路径渲染出的片段形状一致。
    """
    query, category, page = _list_params(request)
    # 文章分区不进记忆卡片流（2026-10-28 两库合并）：文章列表在 /knowledge/，
    # 记忆页只管 fact 条——编辑表单按 500 字短条口径截断，长文进来会被毁掉
    memories = visible_qs(Memory, request.user).exclude(type='document')
    if category:
        memories = memories.filter(category=category)
    if query:
        memories = memories.filter(content__icontains=query)
    memories = memories.order_by('-importance', '-updated_at').prefetch_related(
        # 溯源 UI（ROA Phase C）：来源消息→对话、取代指针、审计时间线一次取齐，
        # 避免 30 张卡片各查三次
        'source_message__conversation', 'superseded_by',
        Prefetch('audits', queryset=MemoryAudit.objects.order_by('-created_at')),
    )

    paginator = Paginator(memories, MEMORY_PAGE_SIZE)
    page_obj = paginator.get_page(page)

    return {
        'memories': page_obj.object_list,
        'page_obj': page_obj,
        'page_numbers': page_window(page_obj.number, paginator.num_pages),
        'page_base_qs': _filter_qs(query, category),
        # 编辑触发 URL 携带当前筛选与页码，取消/保存才能回到进入编辑前的状态
        'edit_params_qs': _filter_qs(query, category, page_obj.number),
        'query': query,
        'category': category,
    }


def _compute_goal_progress(user):
    """计算目标进度：返回 [{content, total, done, percent}, ...]"""
    from django.db.models import Q
    from activities.models import Activity

    goals = visible_qs(Memory, user).filter(category='goal').exclude(type='document')
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
    """记忆列表页（类别筛选 + 搜索 + 分页）"""
    context = _memory_list_context(request)

    # 目标进度（始终计算，模板用 {% if goals %} 控制显示）
    goals = _compute_goal_progress(request.user)

    # HTMX 请求只返回列表片段
    if request.htmx:
        return render(request, 'memory/_memory_items.html', context)

    context.update({
        'categories': Memory.CATEGORY_CHOICES,
        'goals': goals,
    })
    return render(request, 'memory/memory_list.html', context)


@login_required
def memory_edit(request, memory_id):
    """编辑记忆（HTMX 局部渲染）"""
    memory = get_visible(Memory, request.user, id=memory_id)
    if memory.type == 'document':
        # 文章有自己的编辑页（保留版本历史），不进 500 字短条表单
        return redirect('knowledge:article_edit', pk=memory.id)

    if request.method == 'POST':
        # 提交编辑
        content = request.POST.get('content', '').strip()
        category = request.POST.get('category', 'other')
        importance = request.POST.get('importance', 5)

        if content:
            memory.content = content[:MEMORY_CONTENT_MAX_LENGTH]
        if category in dict(Memory.CATEGORY_CHOICES):
            memory.category = category
        try:
            memory.importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            from django.contrib import messages
            messages.warning(request, '重要度必须是 1-10 的整数，已保持原值')
        memory.save()

        if request.htmx:
            # HTMX 返回列表片段：筛选/页码取自表单的 filter_* 隐藏字段（见 _list_params），
            # 保存后的列表与进入编辑前的搜索状态一致
            return render(request, 'memory/_memory_items.html',
                          _memory_list_context(request))
        return redirect('memory:memory_list')

    # GET 请求返回编辑表单：携带当前筛选与页码，供取消按钮的 hx-get URL
    # 与隐藏字段回传使用
    query, category, page = _list_params(request)
    return render(request, 'memory/_memory_edit_form.html', {
        'memory': memory,
        'categories': Memory.CATEGORY_CHOICES,
        'filter_query': query,
        'filter_category': category,
        'filter_page': page,
        'list_params_qs': _filter_qs(query, category, page),
    })


@login_required
@require_POST
def memory_delete(request, memory_id):
    """删除记忆"""
    memory = get_visible(Memory, request.user, id=memory_id)

    memory.delete()

    if request.htmx:
        # 删除按钮是 hx-target="#memory-<id>" + hx-swap="outerHTML"：
        # 空响应恰好移除这一行。返回整份列表片段会把整个列表嵌进这张卡片
        # （UI 走查 blocker），页码/总数留到下一次正常导航时自然刷新
        return HttpResponse('')

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

    all_memories = visible_qs(Memory, request.user).exclude(type='document')

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


@login_required
def memory_export(request):
    """一键导出全部记忆为 Markdown 附件（ROA PRD §9.1：owner 拥有导出权）

    按 type 分组、组内按 importance 排序；包含状态/来源时间等元信息，
    superseded 条目也导出（取代链是「我认为过什么」的完整历史）。
    """
    from django.utils import timezone

    memories = (Memory.objects.filter(user=request.user)
                .order_by('type', '-importance', '-updated_at'))

    type_labels = dict(Memory.TYPE_CHOICES)
    status_labels = dict(Memory.STATUS_CHOICES)
    cat_labels = dict(Memory.CATEGORY_CHOICES)
    lines = [
        f'# 记忆导出 · {timezone.localdate().strftime("%Y-%m-%d")}',
        '',
        f'共 {memories.count()} 条。本文件由三磊自动生成，涵盖全部记忆'
        f'（含已取代/已归档条目）。',
    ]
    current_type = None
    for m in memories:
        if m.type != current_type:
            current_type = m.type
            lines += ['', f'## {type_labels.get(m.type, m.type)}', '']
        meta = (f'[{status_labels.get(m.status, m.status)} · '
                f'{cat_labels.get(m.category, m.category)} · '
                f'重要度 {m.importance} · 更新于 '
                f'{timezone.localdate(m.updated_at).strftime("%Y-%m-%d")}]')
        lines.append(f'- {m.content}')
        lines.append(f'  - {meta}')
        if m.superseded_by_id:
            lines.append(f'  - 被取代为记忆 #{m.superseded_by_id}')

    content = '\n'.join(lines) + '\n'
    filename = f'memories-{timezone.localdate().strftime("%Y%m%d")}.md'
    resp = HttpResponse(content, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp
