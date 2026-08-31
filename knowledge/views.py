from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models
from taggit.models import Tag

from .models import Article
from .forms import ArticleForm
from core.utils import used_tags, visible_qs, get_visible


@login_required
def article_list(request):
    """文章列表，支持标签筛选和关键词搜索"""
    articles = visible_qs(Article, request.user)
    
    # 标签筛选
    tag_slug = request.GET.get('tag')
    if tag_slug:
        articles = articles.filter(tags__slug=tag_slug)
    
    # 关键词搜索
    q = request.GET.get('q', '').strip()
    if q:
        articles = articles.filter(
            models.Q(title__icontains=q) | models.Q(content__icontains=q)
        )
    
    # 获取所有用过的标签（限定 content_type，否则 taggit 的 object_id 会跨模型撞号，
    # 把笔记/活动的标签混进文章标签栏）
    all_tags = used_tags(Article, visible_qs(Article, request.user))
    
    current_tag = Tag.objects.filter(slug=tag_slug).first() if tag_slug else None
    
    return render(request, 'knowledge/article_list.html', {
        'articles': articles,
        'all_tags': all_tags,
        'current_tag': current_tag,
        'query': q,
    })


@login_required
def article_detail(request, slug):
    """文章详情"""
    article = get_visible(Article, request.user, slug=slug)

    # 跨模块关联推荐
    from core.cross_link import get_related_content
    related = get_related_content(request.user, Article, article, limit=5)

    return render(request, 'knowledge/article_detail.html', {
        'article': article,
        'related_activities': related.get('activities', []),
        'related_notes': related.get('notes', []),
    })


@login_required
def article_create(request):
    """创建文章"""
    if request.method == 'POST':
        form = ArticleForm(request.POST)
        if form.is_valid():
            article = form.save(commit=False)
            article.user = request.user
            article.save()
            form.save_m2m()  # 保存标签
            messages.success(request, f'文章「{article.title}」已创建')
            return redirect('knowledge:article_detail', slug=article.slug)
    else:
        form = ArticleForm()
    
    return render(request, 'knowledge/article_form.html', {
        'form': form,
        'title': '新建文章',
    })


@login_required
def article_edit(request, pk):
    """编辑文章"""
    article = get_visible(Article, request.user, pk=pk)
    
    if request.method == 'POST':
        form = ArticleForm(request.POST, instance=article)
        if form.is_valid():
            article = form.save()
            messages.success(request, f'文章「{article.title}」已更新')
            return redirect('knowledge:article_detail', slug=article.slug)
    else:
        form = ArticleForm(instance=article)
    
    return render(request, 'knowledge/article_form.html', {
        'form': form,
        'title': '编辑文章',
        'article': article,
    })


@login_required
def article_delete(request, pk):
    """删除文章"""
    article = get_visible(Article, request.user, pk=pk)
    
    if request.method == 'POST':
        title = article.title
        article.delete()
        messages.success(request, f'文章「{title}」已删除')
        return redirect('knowledge:article_list')
    
    return redirect('knowledge:article_detail', slug=article.slug)
