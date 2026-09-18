"""
简易 RAG 检索：按段落切分文章 → 字符 n-gram 相似度匹配 → 返回 top-K 段落

不引入外部 ML 依赖，用 TF-IDF 风格的字符三元组相似度。
"""
import logging
import math
import re
from collections import Counter

logger = logging.getLogger(__name__)


def _tokenize(text):
    """字符三元组（trigram）分词，中英文通用"""
    text = text.lower().strip()
    if len(text) < 3:
        return [text]
    return [text[i:i + 3] for i in range(len(text) - 2)]


def _cosine_similarity(tokens_a, tokens_b):
    """余弦相似度（TF 向量）"""
    if not tokens_a or not tokens_b:
        return 0.0
    counter_a = Counter(tokens_a)
    counter_b = Counter(tokens_b)
    intersection = set(counter_a.keys()) & set(counter_b.keys())
    dot = sum(counter_a[t] * counter_b[t] for t in intersection)
    mag_a = math.sqrt(sum(v * v for v in counter_a.values()))
    mag_b = math.sqrt(sum(v * v for v in counter_b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def split_into_paragraphs(text):
    """按段落切分文章，过滤空段和过短段"""
    paragraphs = re.split(r'\n\s*\n', text)
    result = []
    for p in paragraphs:
        p = p.strip()
        if len(p) >= 20:  # 过滤太短的段
            result.append(p)
    return result


def retrieve_relevant_paragraphs(query, user, top_k=3):
    """检索与查询最相关的段落

    返回: [{'article_title': str, 'article_slug': str, 'paragraph': str, 'score': float}, ...]
    """
    from knowledge.models import Article
    from core.utils import visible_qs

    articles = visible_qs(Article, user)
    query_tokens = _tokenize(query)

    if not query_tokens:
        return []

    candidates = []
    for article in articles[:50]:  # 限制扫描范围
        paragraphs = split_into_paragraphs(article.content)
        for para in paragraphs:
            para_tokens = _tokenize(para)
            score = _cosine_similarity(query_tokens, para_tokens)
            if score > 0.1:  # 阈值过滤
                candidates.append({
                    'article_title': article.title,
                    'article_slug': article.slug,
                    'paragraph': para[:300],  # 截断过长段落
                    'score': score,
                })

    # 按分数排序取 top-K
    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates[:top_k]


def build_rag_context(query, user):
    """构建 RAG 上下文注入文本，无命中返回空串"""
    try:
        paragraphs = retrieve_relevant_paragraphs(query, user, top_k=3)
        if not paragraphs:
            return ''

        lines = ['[相关知识库内容]']
        for i, p in enumerate(paragraphs, 1):
            lines.append(f'来源{i}：{p["article_title"]}')
            lines.append(p['paragraph'])
            lines.append('')
        lines.append('[/相关知识库内容]')
        lines.append('')
        return '\n'.join(lines)
    except Exception as exc:
        logger.warning('RAG 检索降级: %s', exc)
        return ''
