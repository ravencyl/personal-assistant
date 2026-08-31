"""知识库 Agent 工具集

注册到 core.agent_registry，由对话编排器按意图分发调用。
约定：权限一律按 user 过滤；参数缺失时抛 ToolError 让用户澄清。
"""
from django.urls import reverse

from core.agent_registry import ToolError, agent_tool
from core.utils import visible_qs

from .models import Article
from .utils import search_articles


@agent_tool('knowledge.search', '在用户自己保存的知识库文章里检索（只能查本地存量内容；'
                        '通用知识/时效/攻略类问题不要用它，应直接联网回答或走 ask）',
            'keyword（搜索关键词，必填）+ tag（标签，可选）')
def tool_knowledge_search(user, params):
    keyword = str(params.get('keyword') or '').strip()
    if not keyword:
        raise ToolError('请告诉我搜索关键词')
    tag = str(params.get('tag') or '').strip()

    articles = search_articles(visible_qs(Article, user), keyword, tag=tag, limit=5)
    if not articles:
        # 工具返回的 reply 会直接展示给用户（不会再送回模型），所以只写给用户看的口语，
        # 不能写成对模型的指令；同时给出可操作的下一步（知识库存量以外的信息可以联网问）
        hint = f'（标签：{tag}）' if tag else ''
        return {'reply': f'知识库里没有与「{keyword}」相关的文章{hint}——这一类只能查你自己存进知识库的内容。'
                         '外部信息直接问我就行（例如“上网查一下美国出差要提前准备什么”）。'}

    items = []
    for a in articles:
        summary = a.content[:200].replace('\n', ' ').strip()
        ellipsis = '...' if len(a.content) > 200 else ''
        url = reverse('knowledge:article_detail', kwargs={'slug': a.slug})
        items.append(f'• {a.title}：{summary}{ellipsis}（{url}）')
    return {
        'reply': f'找到 {len(items)} 篇相关知识库文章：\n' + '\n'.join(items),
    }
