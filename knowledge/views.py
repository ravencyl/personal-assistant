import logging

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .forms import ArticleForm
from .models import KnowledgeArticle

logger = logging.getLogger(__name__)


@login_required
def index(request):
    """知识库首页"""
    articles = KnowledgeArticle.objects.published()[:20]

    query = request.GET.get('q', '')
    if query:
        articles = KnowledgeArticle.objects.published().search(query)

    return render(request, 'knowledge/index.html', {
        'articles': articles,
        'query': query,
    })


@login_required
def article_detail(request, pk):
    """文章详情"""
    article = get_object_or_404(KnowledgeArticle, pk=pk)
    return render(request, 'knowledge/article_detail.html', {
        'article': article,
    })


@login_required
def article_create(request):
    """新建文章"""
    if request.method == 'POST':
        form = ArticleForm(request.POST)
        if form.is_valid():
            article = form.save()
            messages.success(request, f'文章「{article.title}」已创建')
            return redirect('knowledge:article_detail', article.pk)
    else:
        form = ArticleForm()

    return render(request, 'knowledge/article_form.html', {
        'form': form,
        'title': '新建文章',
    })


@login_required
def article_edit(request, pk):
    """编辑文章"""
    article = get_object_or_404(KnowledgeArticle, pk=pk)

    if request.method == 'POST':
        form = ArticleForm(request.POST, instance=article)
        if form.is_valid():
            form.save()
            messages.success(request, f'文章「{article.title}」已更新')
            return redirect('knowledge:article_detail', article.pk)
    else:
        form = ArticleForm(instance=article)

    return render(request, 'knowledge/article_form.html', {
        'form': form,
        'title': '编辑文章',
        'article': article,
    })


@login_required
@require_POST
def article_delete(request, pk):
    """删除文章"""
    article = get_object_or_404(KnowledgeArticle, pk=pk)
    title = article.title
    article.delete()
    messages.success(request, f'文章「{title}」已删除')
    return redirect('knowledge:index')


@login_required
@require_POST
def ai_ask(request):
    """AI 知识问答 - 基于知识库内容回答问题"""
    question = request.POST.get('question', '').strip()
    if not question:
        return JsonResponse({'error': '请输入问题'}, status=400)

    # 1. 先通过全文检索找到相关知识片段
    related_articles = list(KnowledgeArticle.objects.published().search(question))[:5]

    if not related_articles:
        return JsonResponse({
            'answer': '知识库中暂未找到相关内容。请尝试添加更多知识文章，或使用 AI 对话功能直接提问。',
            'sources': [],
        })

    # 2. 构建上下文
    context_parts = []
    sources = []
    for article in related_articles:
        body_text = article.body
        # 去除 HTML 标签，保留纯文本
        import re
        clean_text = re.sub(r'<[^>]+>', '', str(body_text))[:1000]
        context_parts.append(f"### {article.title}\n{clean_text}")
        sources.append({
            'title': article.title,
            'pk': article.pk,
        })

    knowledge_context = '\n\n---\n\n'.join(context_parts)

    # 3. 调用 Qoder Cloud Agents 进行知识问答
    try:
        from agents.services import get_service
        from agents.models import AgentConfig, EnvironmentConfig

        service = get_service()

        # 查找 knowledge-agent
        knowledge_agent = AgentConfig.objects.filter(purpose='knowledge', is_active=True).first()
        if not knowledge_agent:
            knowledge_agent = AgentConfig.objects.filter(is_active=True).first()

        if not knowledge_agent:
            return JsonResponse({
                'answer': '请先在管理面板中配置 Agent。',
                'sources': sources,
            })

        env_config = EnvironmentConfig.objects.filter(is_default=True).first()
        if not env_config:
            env_config = EnvironmentConfig.objects.first()

        if not env_config:
            return JsonResponse({
                'answer': '请先在管理面板中配置 Environment。',
                'sources': sources,
            })

        # 创建临时 Session 进行知识问答
        session_data = service.create_session(
            agent_id=knowledge_agent.agent_id,
            environment_id=env_config.env_id,
        )

        # 构建带知识库上下文的问题
        full_question = (
            f"以下是知识库中的相关内容：\n\n{knowledge_context}\n\n"
            f"---\n\n基于以上知识库内容，请回答以下问题：{question}"
        )

        try:
            service.send_message(session_data['id'], full_question)
            answer = service.wait_for_response(session_data['id'], timeout=60)
        finally:
            # 回收一次性 Session，避免在 Qoder 平台累积
            try:
                service.cancel_session(session_data['id'])
            except Exception:
                pass

        if not answer:
            answer = 'AI 处理超时，请稍后重试。'

        return JsonResponse({
            'answer': answer,
            'sources': sources,
        })

    except Exception as e:
        logger.error(f'AI knowledge Q&A failed: {e}')
        return JsonResponse({
            'answer': f'AI 服务暂时不可用，请稍后重试。',
            'sources': sources,
        })
