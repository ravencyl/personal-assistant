import logging

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib import messages

from .models import Bookmark, ContentCategory
from .forms import BookmarkForm
from core.utils import visible_qs, get_visible

logger = logging.getLogger(__name__)


@login_required
def bookmark_list(request):
    """书签列表（超级用户可见全部）"""
    bookmarks = visible_qs(Bookmark, request.user)

    category_filter = request.GET.get('category', '')
    if category_filter:
        bookmarks = bookmarks.filter(category__slug=category_filter)

    categories = ContentCategory.objects.all()

    return render(request, 'content/bookmark_list.html', {
        'bookmarks': bookmarks[:50],
        'categories': categories,
        'category_filter': category_filter,
        'form': BookmarkForm(),
    })


@login_required
@require_POST
def create_bookmark(request):
    """创建书签"""
    form = BookmarkForm(request.POST)
    if form.is_valid():
        bookmark = form.save(commit=False)
        bookmark.user = request.user
        bookmark.save()
        form.save_m2m()  # 保存 tags
        messages.success(request, f'已收藏「{bookmark.title}」')
    else:
        messages.error(request, '添加收藏失败')
    return redirect('content:bookmark_list')


@login_required
def bookmark_detail(request, pk):
    """书签详情"""
    bookmark = get_visible(Bookmark, request.user, pk=pk)
    return render(request, 'content/bookmark_detail.html', {
        'bookmark': bookmark,
    })


@login_required
@require_POST
def delete_bookmark(request, pk):
    """删除书签"""
    bookmark = get_visible(Bookmark, request.user, pk=pk)
    bookmark.delete()
    messages.success(request, '已删除收藏')
    return redirect('content:bookmark_list')


@login_required
@require_POST
def generate_ai_summary(request, pk):
    """为书签生成 AI 摘要"""
    bookmark = get_visible(Bookmark, request.user, pk=pk)

    try:
        from agents.services import get_service
        from agents.models import AgentConfig, EnvironmentConfig

        service = get_service()

        content_agent = AgentConfig.objects.filter(purpose='content', is_active=True).first()
        if not content_agent:
            content_agent = AgentConfig.objects.filter(is_active=True).first()

        if not content_agent:
            return JsonResponse({'error': '请先配置 Agent'}, status=400)

        env_config = EnvironmentConfig.objects.filter(is_default=True).first()
        if not env_config:
            env_config = EnvironmentConfig.objects.first()

        if not env_config:
            return JsonResponse({'error': '请先配置 Environment'}, status=400)

        session_data = service.create_session(
            agent_id=content_agent.agent_id,
            environment_id=env_config.env_id,
        )

        summary_prompt = (
            f'请抓取并总结以下网页的内容，输出 200 字以内的中文摘要：\n\n{bookmark.url}'
        )

        try:
            service.send_message(session_data['id'], summary_prompt)
            summary = service.wait_for_response(session_data['id'], timeout=60)
        finally:
            # 回收一次性 Session，避免在 Qoder 平台累积
            try:
                service.cancel_session(session_data['id'])
            except Exception:
                pass

        if summary:
            bookmark.ai_summary = summary.strip()
            bookmark.save(update_fields=['ai_summary', 'updated_at'])
            return JsonResponse({'success': True, 'summary': summary.strip()})
        else:
            return JsonResponse({'error': '生成摘要超时'}, status=504)

    except Exception as e:
        logger.error(f'AI summary generation failed: {e}')
        return JsonResponse({'error': 'AI 服务暂时不可用'}, status=500)


@login_required
def feed_list(request):
    """RSS 订阅列表"""
    from .models import RSSFeed, FeedItem

    feeds = RSSFeed.objects.all()
    items = FeedItem.objects.filter(feed__in=feeds).order_by('-published_at')[:30]

    return render(request, 'content/feed_list.html', {
        'feeds': feeds,
        'items': items,
    })
