import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from taggit.models import Tag

from .forms import NoteForm
from .models import Note

logger = logging.getLogger(__name__)


def _user_tag_names(user):
    """用户笔记中使用过的全部标签名（供筛选栏展示）"""
    note_ids = Note.objects.filter(user=user).values('id')
    return list(
        Tag.objects.filter(
            taggit_taggeditem_items__content_type=ContentType.objects.get_for_model(Note),
            taggit_taggeditem_items__object_id__in=note_ids,
        )
        .distinct()
        .values_list('name', flat=True)
        .order_by('name')
    )


@login_required
def note_list(request):
    """笔记列表页，支持标签筛选和关键词搜索"""
    qs = Note.objects.filter(user=request.user)

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
    """创建笔记（支持快速创建和完整创建两种入口）"""
    # 判断是否为快速创建（只有 content，无 tags/pinned）
    content = (request.POST.get('content') or '').strip()
    if not content:
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

    messages.success(request, '备忘录已创建')
    return redirect('notes:note_list')


@login_required
def note_edit(request, note_id):
    """编辑笔记"""
    note = get_object_or_404(Note, id=note_id, user=request.user)

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
        'notes': Note.objects.filter(user=request.user).prefetch_related('tags'),
        'all_tags': _user_tag_names(request.user),
    })


@login_required
@require_POST
def note_delete(request, note_id):
    """删除笔记"""
    note = get_object_or_404(Note, id=note_id, user=request.user)
    note.delete()
    messages.success(request, '备忘录已删除')
    return redirect('notes:note_list')


@login_required
@require_POST
def note_toggle_pin(request, note_id):
    """切换置顶状态"""
    note = get_object_or_404(Note, id=note_id, user=request.user)
    note.pinned = not note.pinned
    note.save(update_fields=['pinned', 'updated_at'])
    status = '已置顶' if note.pinned else '已取消置顶'
    messages.success(request, f'备忘录{status}')
    return redirect('notes:note_list')
