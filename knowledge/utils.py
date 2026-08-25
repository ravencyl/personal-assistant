"""知识库检索工具：分词 + 关键词 OR 组合查询

供 chat 知识库上下文注入与 knowledge.search Agent 工具共用。
策略：对用户文本分词（过滤单字与停用词，最多 5 个关键词），
每个关键词做 标题/标签/正文 icontains 的 OR 查询；标题/标签命中优先，正文命中补足。
"""
import re

from django.db import models

# 常见无检索意义的停用词（中英）
STOPWORDS = {
    '的', '了', '是', '在', '我', '你', '他', '她', '它', '有', '和', '与',
    '或', '把', '被', '就', '都', '也', '很', '这', '那', '请', '帮我', '帮忙',
    '什么', '怎么', '一下', '看看', '查查', '搜索', '查找', '有没有',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'and', 'or', 'of', 'to',
    'in', 'on', 'for', 'with', 'my', 'me', 'you', 'please', 'help',
}

_TOKEN_FIND = re.compile(r'[\w\u4e00-\u9fff]{2,}', re.UNICODE)


def tokenize(text, max_tokens=5):
    """提取长度 >=2 的词元（含中文），过滤停用词并去重，最多返回 max_tokens 个关键词"""
    tokens = []
    seen = set()
    for part in _TOKEN_FIND.findall(text or ''):
        key = part.lower()
        if key in seen or key in STOPWORDS:
            continue
        seen.add(key)
        tokens.append(part)
        if len(tokens) >= max_tokens:
            break
    return tokens


def search_articles(qs, text, tag=None, limit=5):
    """在给定 QuerySet（调用方已按可见性过滤）中检索文章

    - tag 非空时先按标签缩小范围
    - 标题/标签命中优先入列，不足再用正文命中补足，总数不超过 limit
    """
    if tag:
        qs = qs.filter(tags__name__icontains=tag)

    tokens = tokenize(text)
    if not tokens:
        return []

    primary, secondary, seen = [], [], set()
    for token in tokens:
        head_q = models.Q(title__icontains=token) | models.Q(tags__name__icontains=token)
        for article in qs.filter(head_q).distinct()[:limit]:
            if article.pk not in seen:
                seen.add(article.pk)
                primary.append(article)
        for article in qs.filter(content__icontains=token)[:limit]:
            if article.pk not in seen:
                seen.add(article.pk)
                secondary.append(article)
        if len(primary) >= limit:
            break

    return (primary + secondary)[:limit]
