from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models
from taggit.models import Tag

from .models import Article
from .forms import ArticleForm


@login_required
def article_list(request):
    """文章列表，支持标签筛选和关键词搜索"""
    articles = Article.objects.filter(user=request.user)
    
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
    
    # 获取所有用过的标签
    all_tags = Tag.objects.filter(
        taggit_taggeditem_items__object_id__in=Article.objects.filter(user=request.user).values_list('id', flat=True)
    ).distinct()
    
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
    article = get_object_or_404(Article, slug=slug, user=request.user)
    return render(request, 'knowledge/article_detail.html', {
        'article': article,
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
    article = get_object_or_404(Article, pk=pk, user=request.user)
    
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
    article = get_object_or_404(Article, pk=pk, user=request.user)
    
    if request.method == 'POST':
        title = article.title
        article.delete()
        messages.success(request, f'文章「{title}」已删除')
        return redirect('knowledge:article_list')
    
    return redirect('knowledge:article_detail', slug=article.slug)
