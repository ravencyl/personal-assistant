"""知识库 Agent 工具集

注册到 core.agent_registry，由对话编排器按意图分发调用。
约定：权限一律按 user 过滤；参数缺失时抛 ToolError 让用户澄清。
"""
from django.urls import reverse

from core.agent_registry import ToolError, agent_tool
from core.utils import visible_qs

from .models import Article
from .utils import search_articles


@agent_tool('knowledge.search', '查询/搜索知识库文章',
            'keyword（搜索关键词，必填）+ tag（标签，可选）')
def tool_knowledge_search(user, params):
    keyword = str(params.get('keyword') or '').strip()
    if not keyword:
        raise ToolError('请告诉我搜索关键词')
    tag = str(params.get('tag') or '').strip()

    articles = search_articles(visible_qs(Article, user), keyword, tag=tag, limit=5)
    if not articles:
        hint = f'（标签：{tag}）' if tag else ''
        return {'reply': f'没有找到与「{keyword}」相关的知识库文章{hint}'}

    items = []
    for a in articles:
        summary = a.content[:200].replace('\n', ' ').strip()
        ellipsis = '...' if len(a.content) > 200 else ''
        url = reverse('knowledge:article_detail', kwargs={'slug': a.slug})
        items.append(f'• {a.title}：{summary}{ellipsis}（{url}）')
    return {
        'reply': f'找到 {len(items)} 篇相关知识库文章：\n' + '\n'.join(items),
    }
