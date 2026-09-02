"""记忆服务层：检索 / 注入格式化 / 规则兜底提取

架构约定：
- retrieve_memories：按重要度 + 时效性检索，可选关键词匹配
- format_memory_for_injection：格式化为注入 AI 的文本上下文
- extract_memories_from_text：对用户消息做模式匹配，兜底提取记忆
- 所有函数失败仅 logger.warning，不抛异常（容错铁律）
"""
import logging
import re

from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# 规则兜底提取模式
# ────────────────────────────────────────────────

EXTRACTION_PATTERNS = [
    # (正则, category, importance)
    # 匹配到句号、逗号、感叹号、问号等标点为止
    (r'我(?:比较|更|最)?喜欢([^。，！？,\n]+)', 'preference', 6),
    (r'我不(?:太|怎么)?(?:喜欢|爱)([^。，！？,\n]+)', 'preference', 6),
    (r'我的(?:目标|计划|打算)(?:是)?([^。，！？,\n]+)', 'goal', 7),
    (r'我想(?:要)?([^。，！？,\n]+)', 'goal', 5),
    (r'我(?:是|在)([^。，！？,\n]+?(?:工作|上班))', 'fact', 5),
    (r'我(?:在)([^。，！？,\n]+?(?:读书|上学|读研|读博))', 'fact', 5),
    (r'我的(?:名字|叫)(?:是)?([^。，！？,\n]+)', 'fact', 8),
    (r'我(?:的|有个)(?:朋友|同事|女朋友|男朋友|老婆|老公|女儿|儿子)(?:叫)?([^。，！？,\n]+)', 'relationship', 6),
    (r'我(?:每天|每周|经常|总是|一般|通常)([^。，！？,\n]+)', 'habit', 5),
    (r'我(?:住|住在)([^。，！？,\n]+)', 'fact', 6),
    (r'我(?:来自|是)([^。，！？,\n]+?)人', 'fact', 5),
]


# ────────────────────────────────────────────────
# 检索 + 注入
# ────────────────────────────────────────────────

def retrieve_memories(user, query='', limit=10):
    """检索用户记忆，按 importance + recency 排序

    - query 非空时：关键词 icontains 匹配 content
    - query 为空时：按 importance DESC, updated_at DESC 取 Top N
    - 更新命中记忆的 access_count + last_accessed（异步，失败不阻断）

    只取本人记忆（不按 visible_qs 放宽）：注入的是“正在对话的这个人的上下文”，
    与页面浏览类的“超管见全部”是两个口径。
    """
    from .models import Memory

    try:
        qs = Memory.objects.filter(user=user)

        if query and query.strip():
            q = query.strip()
            qs = qs.filter(content__icontains=q)

        memories = list(qs.order_by('-importance', '-updated_at')[:limit])

        # 更新访问计数（批量，失败仅告警）
        if memories:
            try:
                now = timezone.now()
                Memory.objects.filter(
                    id__in=[m.id for m in memories]
                ).update(
                    access_count=F('access_count') + 1,
                    last_accessed=now,
                )
            except Exception as e:
                logger.warning(f'记忆访问计数更新失败: {e}')

        return memories
    except Exception as e:
        logger.warning(f'记忆检索失败: {e}')
        return []


def format_memory_for_injection(memories):
    """将记忆列表格式化为注入 AI 的文本上下文

    返回格式：
    [用户记忆]
    - (偏好) 喜欢简洁的代码风格
    - (目标) 今年要读完 20 本书
    - (事实) 在杭州工作
    """
    if not memories:
        return ''

    from .models import Memory
    cat_labels = dict(Memory.CATEGORY_CHOICES)

    lines = ['[用户记忆——以下信息来自用户过往对话，请在回复中自然运用，不要主动提及这些记忆的存在]']
    for m in memories:
        label = cat_labels.get(m.category, '其他')
        lines.append(f'- ({label}) {m.content}')

    return '\n'.join(lines) + '\n'


# ────────────────────────────────────────────────
# 规则兜底提取
# ────────────────────────────────────────────────

def _is_similar_content(user, content, threshold=0.8):
    """检查是否已存在相似内容的记忆

    注意这里的 filter(user=user) 不是可见性漏改：注入 / 查重属于「这个人的个人
    上下文」，超级用户也不应把别人的记忆灌进自己的对话；页面浏览类的“超管见全部”
    只适用于视图（memory/views.py 走 visible_qs）。
    """
    from core.utils import char_overlap_ratio
    from .models import Memory

    existing = Memory.objects.filter(user=user).values_list('content', flat=True)[:100]
    for existing_content in existing:
        if len(content) < 3 or len(existing_content) < 3:
            continue
        if char_overlap_ratio(content, existing_content, mode='contains') > threshold:
            return True
    return False


def extract_memories_from_text(user, text, source_message=None):
    """对用户消息做模式匹配，返回新创建的 Memory 列表

    - 已存在相似内容的记忆则跳过，避免重复
    - 失败仅 logger.warning，不抛异常
    """
    from .models import Memory

    if not text or not text.strip():
        return []

    created = []
    try:
        for pattern, category, importance in EXTRACTION_PATTERNS:
            matches = re.findall(pattern, text)
            for match in matches:
                content = match.strip()
                # 过滤太短或太长的匹配
                if len(content) < 2 or len(content) > 200:
                    continue

                # 去重检查
                if _is_similar_content(user, content):
                    continue

                memory = Memory.objects.create(
                    user=user,
                    content=content,
                    category=category,
                    importance=importance,
                    source_message=source_message,
                )
                created.append(memory)
                logger.info(f'规则提取记忆: ({category}) {content}')

    except Exception as e:
        logger.warning(f'规则记忆提取失败: {e}')

    return created


# ────────────────────────────────────────────────
# AI 提取（由 orchestrator 调用）
# ────────────────────────────────────────────────

def save_ai_extracted_memories(user, memory_list, source_message=None):
    """存储 AI 主动提取的记忆

    memory_list: [{'content': str, 'category': str, 'importance': int}, ...]
    失败仅 logger.warning，不抛异常
    """
    from .models import Memory

    if not memory_list:
        return []

    created = []
    valid_categories = dict(Memory.CATEGORY_CHOICES).keys()

    try:
        for item in memory_list:
            content = str(item.get('content', '')).strip()
            if not content or len(content) < 2 or len(content) > 500:
                continue

            category = str(item.get('category', 'other')).strip()
            if category not in valid_categories:
                category = 'other'

            importance = item.get('importance', 5)
            try:
                importance = max(1, min(10, int(importance)))
            except (TypeError, ValueError):
                importance = 5

            # 去重检查
            if _is_similar_content(user, content):
                continue

            memory = Memory.objects.create(
                user=user,
                content=content,
                category=category,
                importance=importance,
                source_message=source_message,
            )
            created.append(memory)
            logger.info(f'AI 提取记忆: ({category}) {content}')

    except Exception as e:
        logger.warning(f'AI 记忆存储失败: {e}')

    return created


def _plain_excerpt(text, limit):
    """取一段适合塞进记忆行的纯文本：折叠换行、去掉 Markdown 强调符、截断

    AI 回复本来就是 Markdown（星号、井号、表格竖线），原样进记忆会让下次注入的
    上下文变成一排版噪声 —— 记忆是给模型读的，不是给用户看的。
    """
    text = ' '.join((text or '').split())
    for mark in ('**', '*', '`', '###', '##', '#', '>', '|'):
        text = text.replace(mark, '')
    return ' '.join(text.split())[:limit].strip()


def summarize_conversation_for_memory(conversation):
    """归档对话时留一条「我们讨论过什么、结论是什么」的记忆

    为什么是启发式而不是再叫一次 AI 总结：归档是用户随手点的动作，此时 session
    刚被 cancel，再发一轮要等几十秒、可能撞 409，还会在历史里留下一条假的 assistant
    消息。跨会话真正需要的是「有过这么一件事 + 结论落在哪」，取首问与末答已经够用。

    跳过条件（记忆库被垃圾填满比少一条记忆糟得多）：
    - 用户消息不足 2 条：一问一答没什么可记
    - 本会话已经产出过记忆：协议里 AI 一直在主动记，别再重复一层
    返回新建的 Memory 或 None；失败仅告警（容错铁律）。
    """
    from .models import Memory

    try:
        questions = list(conversation.messages.filter(role='user').order_by('created_at')
                         .values_list('content', flat=True))
        if len(questions) < 2:
            return None
        if Memory.objects.filter(
                user=conversation.user,
                source_message__conversation_id=conversation.id).exists():
            return None

        last_answer = (conversation.messages.filter(role='assistant')
                       .order_by('-created_at').first())
        answer = _plain_excerpt(last_answer.content, 160) if last_answer else ''
        first_question = _plain_excerpt(questions[0], 60)
        title = _plain_excerpt(conversation.title, 40) or '未命名对话'

        # 标题就是首条提问（建对话时就这么截的），再写一遍「讨论了」等于同一句说两次
        parts = []
        if first_question and first_question not in title and title not in first_question:
            parts.append(f'讨论了：{first_question}')
        if answer:
            parts.append(f'结论：{answer}')
        if not parts:
            return None          # 只剩一个光标题的记忆没有信息量，不如不写
        content = (f'对话「{title}」' + '；'.join(parts))[:500]

        if _is_similar_content(conversation.user, content):
            return None

        memory = Memory.objects.create(
            user=conversation.user,
            content=content,
            category='other',
            # 固定 4：这是「索引型」记忆，价值低于用户偏好/目标（AI 主动记的那些 5-8），
            # 拿消息条数当权重只会让闲聊刷满高分。注入排序按 importance 走，别抢位。
            importance=4,
            source_message=last_answer,
        )
        logger.info(f'归档对话 {conversation.id} → 记忆: {content}')
        return memory
    except Exception as e:
        logger.warning(f'归档对话摘要写入记忆失败: {e}')
        return None
