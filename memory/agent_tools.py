"""记忆 Agent 工具集

注册到 core.agent_registry，由对话编排器按意图分发调用。
"""
import logging

from django.urls import reverse

from core.agent_registry import (CandidateToolError, ToolError, agent_tool,
                                 parse_pick_index)
from .models import Memory

logger = logging.getLogger(__name__)


@agent_tool('memory.search', '搜索用户的长期记忆（用户之前告诉过你的信息）',
            'query（搜索关键词，必填）+ category（可选，筛选类别：preference/fact/goal/relationship/habit/other）')
def tool_memory_search(user, params):
    """AI 主动搜索记忆，返回匹配结果"""
    query = str(params.get('query') or '').strip()
    if not query:
        raise ToolError('请告诉我搜索关键词')

    category = str(params.get('category') or '').strip()

    qs = Memory.objects.filter(user=user, consolidated=False)

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


@agent_tool('memory.update',
            '修改或忘掉用户的某条长期记忆（用户明确说「更正一下/不对，改成…/把那条忘了」时用）。'
            '只处理用户明确点名的修改：不要因为对话里出现新信息就自动改写旧记忆，'
            '新信息与旧记忆不冲突时优先用 memory 字段新增',
            'target（定位旧记忆的关键词，必填）+ content（新内容，可选）+ '
            'category（preference/fact/goal/relationship/habit/other，可选）+ '
            'importance（1-10，可选）+ void（true 表示忘掉/删除该条，可选）')
def tool_memory_update(user, params):
    """修改/删除一条已有记忆（用户点名「更正」「忘掉」时用）

    定位与候选重放对齐 knowledge.update 的模式：target 关键词匹配 →
    多条命中抛 CandidateToolError 出候选卡；候选点选/序号重放经
    target_id 直达（pick_candidate 与编排器序号重放都传它）。
    """
    target = str(params.get('target') or '').strip()

    new_content = str(params.get('content') or '').strip()
    category = str(params.get('category') or '').strip()
    importance_raw = params.get('importance')
    void = bool(params.get('void') or params.get('forget'))
    if not new_content and not category and importance_raw is None and not void:
        raise ToolError('请告诉我要改什么（新内容/类别/重要度），或者说「忘掉这条」')

    def _apply(memory):
        """把已定位的记忆套用变更（候选重放与关键词匹配两条路共用）"""
        if void:
            name = memory.content
            memory.delete()
            logger.info(f'AI 忘掉记忆: ({memory.category}) {name}')
            return {'reply': f'已忘掉这条记忆：「{name}」'}

        changes = []
        if new_content:
            if len(new_content) < 2 or len(new_content) > 500:
                raise ToolError('记忆内容长度需在 2-500 字之间')
            if new_content != memory.content:
                changes.append(f'内容「{memory.content}」→「{new_content}」')
                memory.content = new_content
        if category and category != memory.category:
            valid_categories = dict(Memory.CATEGORY_CHOICES).keys()
            if category not in valid_categories:
                raise ToolError('类别只能是 preference/fact/goal/relationship/habit/other')
            changes.append(f'类别「{memory.get_category_display()}」→'
                           f'「{dict(Memory.CATEGORY_CHOICES)[category]}」')
            memory.category = category
        if importance_raw is not None:
            try:
                importance = max(1, min(10, int(importance_raw)))
            except (TypeError, ValueError):
                raise ToolError('重要度需要是 1-10 的数字')
            if importance != memory.importance:
                changes.append(f'重要度 {memory.importance} → {importance}')
                memory.importance = importance
        memory.save()
        logger.info(f'AI 修改记忆: {"; ".join(changes)}')
        return {'reply': f'已更新记忆：{"; ".join(changes)}'}

    # 候选点选/序号重放：按 id 直达（目标已被删时明确报错，不静默换目标）
    target_id = params.get('target_id')
    if not target and not target_id:
        raise ToolError('请告诉我要改哪条记忆（关键词）')
    if target_id:
        memory = Memory.objects.filter(user=user, id=target_id).first()
        if not memory:
            raise ToolError('这条记忆已不存在（可能已被忘掉）')
        return _apply(memory)

    # 用户/模型带序号（「第一个」）：从最近一轮候选快照取 id
    pick = parse_pick_index(params.get('pick'))
    if pick is not None:
        from chat.models import Message
        items = Message.latest_pick_items(user, tool_prefix='memory.')
        if items:
            if not 1 <= pick <= len(items):
                raise ToolError(f'上一轮候选只有 {len(items)} 条，你要的'
                                f'「第 {pick} 条」对不上；请再说一次是哪一条')
            memory = Memory.objects.filter(user=user, id=items[pick - 1]['id']).first()
            if not memory:
                raise ToolError('这条记忆已不存在（可能已被忘掉）')
            return _apply(memory)
        # 没有可用的候选上下文：忽略序号按关键词继续，宁慢勿错

    qs = (Memory.objects.filter(user=user, consolidated=False,
                                content__icontains=target)
          .order_by('-importance', '-updated_at'))
    count = qs.count()
    if count == 0:
        raise ToolError(f'没有找到内容包含「{target}」的记忆——'
                        '如果这是新信息，直接告诉我即可（我会记下来）')
    if count > 1:
        cat_labels = dict(Memory.CATEGORY_CHOICES)
        candidates = [{
            'id': m.id,
            'name': m.content,
            'status': '',
            'status_label': cat_labels.get(m.category, '其他'),
            'date_label': m.updated_at.strftime('%m-%d 更新'),
            'detail_url': reverse('memory:memory_edit', args=[m.id]),
        } for m in qs[:5]]
        raise CandidateToolError(
            f'匹配到 {count} 条包含「{target}」的记忆：点下方候选卡的「选它」，'
            f'或直接回复「第一条」「第二条」（{count} 条里选一条）',
            candidates)

    return _apply(qs.first())
