"""知识库 Agent 工具集

注册到 core.agent_registry，由对话编排器按意图分发调用。
约定：权限一律按 user 过滤；参数缺失时抛 ToolError 让用户澄清。
写入（knowledge.create / knowledge.update）不做两步确认，与 notes.create /
activities.create 口径一致；目标不唯一的修改才需要预览卡（CandidateToolError）。
"""
import re
from urllib.parse import unquote

from django.urls import reverse

from core.agent_registry import CandidateToolError, ToolError, agent_tool
from core.utils import visible_qs

from .models import Article
from .utils import search_articles

# 未展开的协议引用标记（$LAST_REPLY / $LAST_USER），定义见 core.agent_registry
_UNRESOLVED_REF = re.compile(r'^\$LAST_(REPLY|USER)$')


def _article_url(article):
    """站内文章链接（可读版）

    AI 回复在模板里是按纯文本渲染的（既不走 markdown 也不走 urlize），而 reverse()
    会把中文 slug 百分号编码成一串 %E7%BE%8E...，用户读不了也复制不了；
    路由用的是 <str:slug>，所以未转码的中文路径本身是合法可点的。
    """
    return unquote(reverse('knowledge:article_detail', kwargs={'slug': article.slug}))


@agent_tool('knowledge.search', '在用户自己保存的知识库文章里检索（语义检索，能命中近义表达；'
                        '只能查本地存量内容，通用知识/时效/攻略类问题不要用它，应直接联网回答或走 ask）',
            'keyword（搜索关键词或自然语言问题，必填）+ tag（标签，可选）')
def tool_knowledge_search(user, params):
    keyword = str(params.get('keyword') or '').strip()
    if not keyword:
        raise ToolError('请告诉我搜索关键词')
    tag = str(params.get('tag') or '').strip()

    if tag:
        # 带标签条件时走本地检索（QMind 不支持按站内标签过滤）
        articles = search_articles(visible_qs(Article, user), keyword, tag=tag, limit=5)
        hits = [{'title': a.title, 'content': a.content, 'score': None, 'article': a}
                for a in articles]
    else:
        from .retrieval import search_knowledge

        hits = search_knowledge(user, keyword, limit=5)

    if not hits:
        # 工具返回的 reply 会直接展示给用户（不会再送回模型），所以只写给用户看的口语，
        # 不能写成对模型的指令；同时给出可操作的下一步（知识库存量以外的信息可以联网问）
        hint = f'（标签：{tag}）' if tag else ''
        return {'reply': f'知识库里没有与「{keyword}」相关的文章{hint}——这一类只能查你自己存进知识库的内容。'
                         '外部信息直接问我就行（例如“上网查一下美国出差要提前准备什么”）。'}

    items = []
    for h in hits:
        summary = h['content'][:200].replace('\n', ' ').strip()
        ellipsis = '...' if len(h['content']) > 200 else ''
        url = _article_url(h['article']) if h.get('article') else ''
        suffix = f'（{url}）' if url else ''
        items.append(f'• {h["title"]}：{summary}{ellipsis}{suffix}')
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
                             '把刚才那段结论沉淀下来”时用）。'
                             '正文是本轮对话里已经出现过的长内容时，content 直接写引用标记 "$LAST_REPLY"，'
                             '不要重新抄写（重抄长文会被回复长度上限截断）；'
                             '只有全新内容才自己组织完整正文，不要写“详见上文”这类指代',
            'title（标题，必填）+ content（Markdown 正文，必填；引用上一轮回复写 "$LAST_REPLY"）+ tags（标签数组，可选）')
def tool_knowledge_create(user, params):
    title = str(params.get('title') or params.get('name') or '').strip()
    content = str(params.get('content') or '').strip()
    if not title:
        raise ToolError('请给这篇知识库文章一个标题')
    if not content:
        raise ToolError('请告诉我要存进去的内容（可以直接说“把刚才那段结论整理成文章”）')
    if _UNRESOLVED_REF.match(content):
        # 编排器已经展开过引用（见 core.agent_registry.resolve_params_refs）；
        # 走到这里说明调用方没递引用池（报告生成、测试等旁路）。宁可报错也不把
        # “$LAST_REPLY” 十个字符当成文章正文写进库——那是看起来成功的脏数据。
        raise ToolError('没能取到要保存的上文内容，请把正文完整发一次')
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


@agent_tool('knowledge.update', '更新用户知识库里的已有文章（用户说“更新/修改/补充/完善《XX》那篇文章”时用）。'
                             '只处理用户明确点名的修改，不要因为对话里出现新信息就自动改写文章；'
                             '正文是本轮对话里已经出现过的长内容时写引用标记 "$LAST_REPLY"，不要重新抄写',
            'target（目标文章标题关键词，必填）+ title（新标题，可选）+ '
            'content（要写入的 Markdown 正文，可选）+ '
            'content_mode（"append" 追加到文末（默认）| "replace" 整段替换，可选）+ '
            'tags（要追加的标签数组，可选）')
def tool_knowledge_update(user, params):
    target = str(params.get('target') or params.get('name') or '').strip()
    if not target:
        raise ToolError('请告诉我要更新哪篇文章（标题关键词）')

    new_title = str(params.get('title') or '').strip()
    content = str(params.get('content') or '').strip()
    mode = str(params.get('content_mode') or params.get('mode') or 'append').strip().lower()
    tags = _parse_tags(params.get('tags'))
    if not new_title and not content and not tags:
        raise ToolError('请告诉我要改什么（新标题 / 要补充的正文 / 标签）')
    if content and _UNRESOLVED_REF.match(content):
        # 与 create 同款防呆：编排器已展开引用仍收到裸标记，宁可报错也不把
        # "$LAST_REPLY" 十个字符写进文章
        raise ToolError('没能取到要写入的上文内容，请把正文完整发一次')
    if mode not in ('append', 'replace'):
        mode = 'append'
    if mode == 'replace' and content and len(content) < 10:
        # 整段替换成一小句话多半是模型没把上下文展开，宁可拒绝
        raise ToolError('替换后的正文太短，请补完整内容；只是补充几句话请用追加模式')

    qs = visible_qs(Article, user).filter(title__icontains=target).order_by('-updated_at')
    count = qs.count()
    if count == 0:
        raise ToolError(f'知识库里没有标题包含「{target}」的文章——'
                        '如果是新内容，请说「存进知识库」新建一篇')
    if count > 1:
        candidates = [{
            'id': a.id,
            'name': a.title,
            'status': '',
            'status_label': '',
            'date_label': a.updated_at.strftime('%m-%d 更新'),
            'detail_url': _article_url(a),
        } for a in qs[:5]]
        raise CandidateToolError(
            f'匹配到 {count} 篇标题包含「{target}」的文章，请告诉我要更新哪一篇',
            candidates)

    article = qs.first()
    changes = []
    if new_title and new_title != article.title:
        changes.append(f'标题「{article.title}」→「{new_title}」')
        article.title = new_title[:255]
    if content:
        if mode == 'replace':
            changes.append('正文整段替换')
            article.content = content
        else:
            changes.append(f'文末追加了 {len(content)} 字')
            article.content = article.content.rstrip() + '\n\n' + content
    if tags:
        existing = set(article.tags.values_list('name', flat=True))
        fresh = [t for t in tags if t not in existing]
        if fresh:
            article.tags.add(*fresh)
            changes.append('追加标签：' + '、'.join(fresh))
    if not changes:
        return {'reply': f'《{article.title}》已经是最新内容，没有需要修改的地方',
                'changed': False}

    article.save()  # post_save 信号会把变更同步到 QMind 云端镜像（若已配置）
    return {
        'reply': f'已更新《{article.title}》（{_article_url(article)}）：' + '；'.join(changes),
        'changed': True,
    }
