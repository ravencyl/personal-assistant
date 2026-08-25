"""备忘录 Agent 工具集

注册到 core.agent_registry，由对话编排器按意图分发调用。
约定：权限一律按 user 过滤；目标不明确时抛 ToolError 让用户澄清。
"""
from core.agent_registry import ToolError, agent_tool
from .models import Note


@agent_tool('notes.create', '创建一条备忘录',
            'content（内容，必填）+ tags（标签数组，可选）')
def tool_notes_create(user, params):
    content = str(params.get('content') or '').strip()
    if not content:
        raise ToolError('请告诉我备忘录的内容')

    note = Note.objects.create(user=user, content=content)
    tags = params.get('tags') or []
    if tags:
        note.tags.add(*tags)

    return {
        'reply': f'已创建备忘录：「{content[:50]}{"..." if len(content) > 50 else ""}」',
    }


@agent_tool('notes.search', '搜索备忘录',
            'query（搜索关键词，必填）')
def tool_notes_search(user, params):
    query = str(params.get('query') or '').strip()
    if not query:
        raise ToolError('请告诉我搜索关键词')

    notes = Note.objects.filter(user=user, content__icontains=query).order_by('-updated_at')[:5]
    if not notes:
        return {'reply': f'没有找到包含「{query}」的备忘录'}

    items = [
        f'• {n.content[:60]}{"..." if len(n.content) > 60 else ""}（{n.updated_at.strftime("%m-%d")}）'
        for n in notes
    ]
    return {
        'reply': f'找到 {len(items)} 条相关备忘录：\n' + '\n'.join(items),
    }
