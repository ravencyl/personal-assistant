"""动态开场 chips：按用户当前上下文生成对话起头

为什么动态：聊天页的快捷 chips 原来是写死的四条，天天一样就没有点开的理由
（线上实测 2026-09-15：近 14 天仅 25 条用户消息，6 天零使用）。「想话题」是
最大的使用门槛——把 chips 换成每天针对今天的具体问题，点一下就开聊。

数据源（各一条轻量查询，无 AI 调用，页面渲染路径不能变慢）：
1. 今日日程：有活动就问今天的事，比泛泛的「今天有什么安排」具体得多
2. 未完成活动：进行中/过期未完成的活动数
3. 上次话题：3 天内最近的对话标题，接住「聊了一半」的上下文

动态条目优先，不足 limit 用固定兜底条凑（含记忆入口「你还记得我哪些事」，
记忆系统刚补齐「能记、能查、能改」，给它一个常驻入口让用户感知成长）；
完全没有上下文时退化为原来的固定 chips（空库新用户不至于没有东西可点）。

放 core 而不是 chat：要跨 activities/chat/memory 三个 app 查数，core 本就是
横切查询层（cross_link / search / report 同款模式），chat 只消费结果。
"""
from django.utils import timezone
from django.db import models

from chat.models import Conversation, Message
from core.utils import visible_qs

CHIP_LIMIT = 4

# 兜底条：没有任何上下文命中时凑数（与原固定 chips 口径一致，只填不发）
FALLBACK_CHIPS = [
    '今天有什么安排？',
    '接下来我该做什么？',
    '本周花费帮我汇总一下',
    '你还记得我哪些事？',
]

# 上次话题只取 3 天内的对话，更久远的「继续聊聊」反而突兀
RECENT_CONVERSATION_DAYS = 3

# 续聊提醒：对话超过此天数无新消息且 AI 最后回复含待办暗示时提醒
FOLLOW_UP_STALE_DAYS = 3

# 话题标题在 chip 里太长会溢出按钮（.chat-chip 移动端 nowrap），截断保护
TOPIC_MAX_LEN = 12


def get_quick_chips(user, limit=CHIP_LIMIT):
    """返回开场 chip 文本列表（动态优先，兜底凑满）"""
    from activities.models import Activity

    chips = []
    today = timezone.localdate()

    # 1) 今日日程（排除已取消）
    today_qs = visible_qs(Activity, user).filter(
        start_date=today).exclude(status='cancelled')
    today_count = today_qs.count()
    if today_count == 1:
        chips.append(f'今天「{today_qs.first().name}」要注意什么？')
    elif today_count > 1:
        chips.append(f'今天有 {today_count} 个活动，帮我捋一遍安排')

    # 2) 未完成活动：进行中的 + 过了开始日期还没动的
    undone_count = visible_qs(Activity, user).filter(
        status='in_progress').count()
    overdue_count = visible_qs(Activity, user).filter(
        status='planned', start_date__lt=today).count()
    undone_total = undone_count + overdue_count
    if undone_total:
        chips.append(f'我有 {undone_total} 个活动没完成，哪个该先动？')

    # 3) 上次话题：3 天内最近的非归档对话
    since = timezone.now() - timezone.timedelta(days=RECENT_CONVERSATION_DAYS)
    last_conv = (visible_qs(Conversation, user)
                 .exclude(status='archived')
                 .filter(created_at__gte=since)
                 .order_by('-created_at')
                 .first())
    if last_conv:
        topic = (last_conv.title or '').strip()[:TOPIC_MAX_LEN]
        if topic:
            chips.append(f'继续聊聊「{topic}」')
    
    # 3.5) 续聊提醒：超过 FOLLOW_UP_STALE_DAYS 天无新消息且 AI 最后回复含待办暗示
    stale_chip = _stale_follow_up_chip(user)
    if stale_chip and stale_chip not in chips:
        chips.append(stale_chip)
    
    # 4) 兆底凑满：跳过与动态条重复的文本，保持顺序
    for chip in FALLBACK_CHIPS:
        if len(chips) >= limit:
            break
        if chip not in chips:
            chips.append(chip)

    return chips[:limit]


def _stale_follow_up_chip(user, limit=1):
    """检测 stale 对话并生成续聊 chip（超过 FOLLOW_UP_STALE_DAYS 天无消息 + AI 含待办暗示）

    返回 chip 文本或空串。失败静默降级（不阻断 chips 渲染）。
    """
    try:
        stale = get_stale_conversations(user, limit=limit)
        if not stale:
            return ''
        conv = stale[0]
        topic = (conv.title or '').strip()[:TOPIC_MAX_LEN]
        if topic:
            return f'继续聊聊「{topic}」——上次 AI 说有待办'
        return '有个对话还没聊完，上次 AI 说有待办'
    except Exception:
        return ''


def get_stale_conversations(user, limit=3):
    """返回待续聊的对话列表（超过 FOLLOW_UP_STALE_DAYS 天无新消息 + AI 最后回复含待办暗示）

    供对话列表页「待续聊」提示条和 chips 共用。失败返回空列表。
    """
    try:
        since = timezone.now() - timezone.timedelta(days=FOLLOW_UP_STALE_DAYS)
        stale_qs = (visible_qs(Conversation, user)
                    .exclude(status='archived')
                    .filter(updated_at__lte=since)
                    .order_by('-updated_at'))

        # 预取最后一条消息（与 conversation_list 同口径）
        stale_qs = stale_qs.prefetch_related(
            models.Prefetch(
                'messages',
                queryset=Message.objects.order_by('-created_at')[:1],
                to_attr='last_message_list',
            )
        )

        result = []
        for conv in stale_qs[:limit * 3]:  # 多取一些，过滤后可能不足
            if conv.follow_up_hint:
                result.append(conv)
                if len(result) >= limit:
                    break
        return result
    except Exception:
        return []
