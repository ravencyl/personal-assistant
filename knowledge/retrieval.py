"""知识库统一检索层：QMind 语义检索优先，本地关键词兜底

消费方：chat.views._build_knowledge_context（每条消息的知识注入）与
knowledge.search agent 工具。两边都不感知后端差异——拿到的都是
[{'title','content','score','article'}] 统一结构。

降级规则（容错铁律）：
- QMind 未配置（QMIND_NOTEBOOK_ID 为空）→ 直接走本地
- QMind 调用失败/超时（5s）→ logger.warning 后走本地
- QMind 命中但片段关联不到本地文章 → article 为 None，不返回链接（内容照给）
"""
import logging

from core.utils import visible_qs

logger = logging.getLogger(__name__)


def search_knowledge(user, query, limit=3):
    """按查询意图检索用户自己的知识库，返回统一结构的结果列表"""
    results = _search_qmind(user, query, limit)
    if results is None:
        results = _search_local(user, query, limit)
    return results


def _search_qmind(user, query, limit):
    """QMind 语义检索。返回 None 表示「走本地降级」，[] 表示「命中为空」"""
    from . import qmind

    if not qmind.configured():
        return None
    try:
        chunks = qmind.retrieve(query, top_k=limit)
    except Exception as e:
        logger.warning(f'QMind 检索失败，降级本地关键词: {e}')
        return None
    if chunks is None:
        return None

    articles = {a.title: a for a in visible_qs(_article_model(), user)}
    results = []
    for c in chunks:
        # uri 末段即上传时的文件名（= 文章标题），借它把片段挂回本地文章拿深链
        article = articles.get(c['title']) or _match_article(articles, c['title'])
        results.append({
            'title': article.title if article else c['title'],
            'content': c['snippet'],
            'score': c['score'],
            'article': article,
        })
    return results


def _search_local(user, query, limit):
    from .utils import search_articles

    qs = search_articles(visible_qs(_article_model(), user), query, limit=limit)
    return [
        {'title': a.title, 'content': a.content, 'score': None, 'article': a}
        for a in qs
    ]


def _article_model():
    from .models import Article
    return Article


def _match_article(articles, title):
    """精确匹配失败后做包含匹配（云端标题与本地标题可能有 .md 尾巴等微差）"""
    if not title:
        return None
    for name, article in articles.items():
        if title in name or name in title:
            return article
    return None
