"""记忆 Agent 工具集

注册到 core.agent_registry，由对话编排器按意图分发调用。
"""
from core.agent_registry import ToolError, agent_tool
from .models import Memory


@agent_tool('memory.search', '搜索用户的长期记忆（用户之前告诉过你的信息）',
            'query（搜索关键词，必填）+ category（可选，筛选类别：preference/fact/goal/relationship/habit/other）')
def tool_memory_search(user, params):
    """AI 主动搜索记忆，返回匹配结果"""
    query = str(params.get('query') or '').strip()
    if not query:
        raise ToolError('请告诉我搜索关键词')

    category = str(params.get('category') or '').strip()

    qs = Memory.objects.filter(user=user)

    # 关键词匹配
    qs = qs.filter(content__icontains=query)

    # 类别筛选
    valid_categories = dict(Memory.CATEGORY_CHOICES).keys()
    if category and category in valid_categories:
        qs = qs.filter(category=category)

    memories = list(qs.order_by('-importance', '-updated_at')[:5])

    if not memories:
        return {'reply': f'没有找到与「{query}」相关的记忆'}

    cat_labels = dict(Memory.CATEGORY_CHOICES)
    items = []
    for m in memories:
        label = cat_labels.get(m.category, '其他')
        items.append(f'- ({label}) {m.content}')

    return {
        'reply': f'找到 {len(memories)} 条相关记忆：\n' + '\n'.join(items),
    }
