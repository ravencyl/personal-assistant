"""知识库 Agent 工具集

注册到 core.agent_registry，由对话编排器按意图分发调用。
约定：权限一律按 user 过滤；参数缺失时抛 ToolError 让用户澄清。
写入（knowledge.create）不做两步确认，与 notes.create / activities.create 口径一致；
目标不唯一的修改才需要预览卡。
"""
import re
from urllib.parse import unquote

from django.urls import reverse

from core.agent_registry import ToolError, agent_tool
from core.utils import visible_qs

from .models import Article
from .utils import search_articles


def _article_url(article):
    """站内文章链接（可读版）

    AI 回复在模板里是按纯文本渲染的（既不走 markdown 也不走 urlize），而 reverse()
    会把中文 slug 百分号编码成一串 %E7%BE%8E...，用户读不了也复制不了；
    路由用的是 <str:slug>，所以未转码的中文路径本身是合法可点的。
    """
    return unquote(reverse('knowledge:article_detail', kwargs={'slug': article.slug}))


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
        url = _article_url(a)
        items.append(f'• {a.title}：{summary}{ellipsis}（{url}）')
    return {
        'reply': f'找到 {len(items)} 篇相关知识库文章：\n' + '\n'.join(items),
    }


def _parse_tags(raw):
    """标签入参容错：数组或「a、b，c」字符串都接得下（分隔符与 activities 口径一致）"""
    if isinstance(raw, str):
        parts = re.split(r'[,，、；;\n]+', raw)
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = []
    out, seen = [], set()
    for part in parts:
        name = str(part).strip()[:30]
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out[:5]


@agent_tool('knowledge.create', '把一段内容存成一篇知识库文章（用户说“存进知识库 / 记成文章 / '
                             '把刚才那段结论沉淀下来”时用）。正文由你自己根据当前对话整理完整，'
                             '不要写“详见上文”这类指代；成功后会向用户给出文章链接',
            'title（标题，必填）+ content（Markdown 正文，必填，自己整理完整）+ tags（标签数组，可选）')
def tool_knowledge_create(user, params):
    title = str(params.get('title') or params.get('name') or '').strip()
    content = str(params.get('content') or '').strip()
    if not title:
        raise ToolError('请给这篇知识库文章一个标题')
    if not content:
        raise ToolError('请告诉我要存进去的内容（可以直接说“把刚才那段结论整理成文章”）')
    if len(content) < 10:
        # 太短不像一篇可复用的文章，多半是模型没把上下文展开成正文
        raise ToolError('内容太短，存进去以后也查不出什么；请补完整或说明要保存哪一段结论')

    # slug 由 Article.save() 按标题自动生成并消重（中文 slug 走 allow_unicode），不手拼
    article = Article.objects.create(user=user, title=title[:255], content=content)
    tags = _parse_tags(params.get('tags'))
    if tags:
        article.tags.add(*tags)

    url = _article_url(article)
    tag_note = f'，标签：{"、".join(tags)}' if tags else ''
    return {
        'reply': f'已存入知识库：《{article.title}》（{url}）{tag_note}',
        'changed': True,
    }
