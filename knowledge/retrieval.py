"""知识库本地检索层（2026-11-09：QMind 云端检索按用户决定下线，检索只用本地知识库）

消费方：chat.views._build_knowledge_context（每条消息的知识注入）与
knowledge.search agent 工具。返回结构保持统一 [{'title','content','score','article'}]，
消费方不感知后端差异；score 恒为 None（本地关键词命中没有相似度分值），
历史调用都只读 title/content/article，不受影响。

检索策略（纯本地、零外呼）：
- knowledge.utils.tokenize 把查询切成实词关键词（过滤停用词、短词优先）
- 标题/标签命中优先、正文命中补足（knowledge.utils.search_articles）
"""
from core.utils import visible_qs


def search_knowledge(user, query, limit=3, tag=None):
    """按查询意图检索用户自己的知识库（本地文章分区），返回统一结构的结果列表

    tag 非空时先按标签缩小范围（原 QMind 不支持标签过滤而存在的分叉路径，
    下线后统一收进本函数，调用方不必再自己分叉）。
    """
    from .utils import search_articles

    articles = search_articles(visible_qs(_article_model(), user), query,
                               tag=tag, limit=limit)
    return [
        {'title': a.title, 'content': a.content, 'score': None, 'article': a}
        for a in articles
    ]


def _article_model():
    from .models import Document
    return Document
