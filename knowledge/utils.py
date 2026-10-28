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

# 中文高频虚词/功能语素：把连续中文串劈开用（没有词典分词的轻量替代）。
# 整句「我下周要去美国出差」去 icontains 必然落空——按这些虚词切出
# 「下周 / 美国 / 出差」这样的实词片段才有召回。
_CJK_SPLIT = re.compile(
    r'我|你|他|她|它|吗|呢|吧|啊|的|了|是|在|有|和|与|就|把|被|都|也|很|这|那|'
    r'请|帮我|帮忙|什么|怎么|一下|看看|查查|还是|以及|而且|因为|所以|如果|但是|'
    r'然后|还有|关于|之前|上次|最近|那些|哪些|这个|那个|自己|可以|需要|应该|'
    r'想要|想想|说说|聊聊|建议|麻烦|谢谢|现在|今年|去年|明年|今天|明天|后天|昨天',
    re.UNICODE)


def _split_cjk(chunk):
    """长中文串切出候选词：按虚词劈开，保留 ≥2 字的片段；切不出就整块保留"""
    if len(chunk) < 4 or not re.search(r'[\u4e00-\u9fff]', chunk):
        return [chunk]
    parts = [p for p in _CJK_SPLIT.split(chunk) if len(p) >= 2]
    return parts or [chunk]


def tokenize(text, max_tokens=5):
    """提取长度 >=2 的词元（含中文），过滤停用词并去重，最多返回 max_tokens 个关键词

    中文没有空格分词：先按标点/非词字符切出长串，长中文串再按高频虚词劈成
    实词片段；候选按「短词优先」排序后截断（短词更像真词，icontains 召回高，
    长串碎片挤占名额反而伤检索）。
    """
    candidates = []
    for part in _TOKEN_FIND.findall(text or ''):
        candidates.extend(_split_cjk(part))
    tokens = []
    seen = set()
    for part in sorted(candidates, key=len):
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
