import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .forms import NoteForm
from .models import Note
from core.utils import used_tag_names, visible_qs, get_visible
from core.utils import wants_json as is_json_request

logger = logging.getLogger(__name__)


def _user_tag_names(user):
    """可见笔记中使用过的全部标签名（供筛选栏展示）

    走 visible_qs 而不是 filter(user=)：超级用户要能看到全部笔记的标签，
    与活动模块同一口径（AGENTS.md 数据可见性规则）。
    """
    return used_tag_names(Note, visible_qs(Note, user))


@login_required
def note_list(request):
    """笔记列表页，支持标签筛选和关键词搜索"""
    qs = visible_qs(Note, request.user)

    # 标签筛选
    tag_filter = request.GET.get('tag', '').strip()
    if tag_filter:
        qs = qs.filter(tags__name__in=[tag_filter])

    # 关键词搜索
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(content__icontains=q)

    notes = qs.prefetch_related('tags')

    return render(request, 'notes/note_list.html', {
        'notes': notes,
        'tag_filter': tag_filter,
        'all_tags': _user_tag_names(request.user),
        'q': q,
    })


@login_required
@require_POST
def note_create(request):
    """创建笔记（支持快速创建和完整创建两种入口；JSON 请求返回 JSON）"""
    # JSON 客户端（全局快记浮层等，原生 fetch + Accept 头）
    wants_json = is_json_request(request)

    content = (request.POST.get('content') or '').strip()
    if not content:
        if wants_json:
            return JsonResponse({'error': '内容不能为空'}, status=400)
        messages.error(request, '内容不能为空')
        return redirect('notes:note_list')

    # 快速创建：只传 content
    tags_str = request.POST.get('tags', '').strip()
    pinned = request.POST.get('pinned') == 'on'

    note = Note.objects.create(
        user=request.user,
        content=content,
        pinned=pinned,
    )

    # 处理标签（逗号分隔）
    if tags_str:
        tag_names = [t.strip() for t in tags_str.split(',') if t.strip()]
        if tag_names:
            note.tags.add(*tag_names)

    if wants_json:
        return JsonResponse({'success': True, 'id': note.id, 'content': note.content})

    messages.success(request, '备忘录已创建')
    return redirect('notes:note_list')


@login_required
def note_edit(request, note_id):
    """编辑笔记"""
    note = get_visible(Note, request.user, id=note_id)

    if request.method == 'POST':
        form = NoteForm(request.POST, instance=note)
        if form.is_valid():
            form.save()
            messages.success(request, '备忘录已更新')
            return redirect('notes:note_list')
    else:
        form = NoteForm(instance=note)

    return render(request, 'notes/note_list.html', {
        'edit_note': note,
        'edit_form': form,
        'notes': visible_qs(Note, request.user).prefetch_related('tags'),
        'all_tags': _user_tag_names(request.user),
    })


@login_required
@require_POST
def note_delete(request, note_id):
    """删除笔记"""
    note = get_visible(Note, request.user, id=note_id)
    note.delete()
    messages.success(request, '备忘录已删除')
    return redirect('notes:note_list')


@login_required
@require_POST
def note_toggle_pin(request, note_id):
    """切换置顶状态"""
    note = get_visible(Note, request.user, id=note_id)
    note.pinned = not note.pinned
    note.save(update_fields=['pinned', 'updated_at'])
    status = '已置顶' if note.pinned else '已取消置顶'
    messages.success(request, f'备忘录{status}')
    return redirect('notes:note_list')
