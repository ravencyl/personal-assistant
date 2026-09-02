"""Agent 工具注册表与对话编排

- 工具注册：各 app 在自己的 agent_tools.py 中用 @agent_tool 装饰器注册，
  core.apps.ready() 自动发现，新增能力无需改动核心对话逻辑
- 意图协议：首帧指令 prompt 由注册表 + INTENT_TOOL_MAP 动态生成；
  AI 回复解析为 {"intent", "params", "reply"} JSON 后分发执行
- 两类消息分流：操作站点数据→意图协议 JSON；通用问答/需要外部信息→模型自己
  调云端联网工具（WebSearch/WebFetch）后以自然语言回，由本编排器的“非 JSON 透传”分支接手
- 容错约定：任何环节失败都降级为普通文本回复，绝不阻断对话
"""
import hmac
import logging
import re
from hashlib import sha256

from django.conf import settings
from django.utils import timezone

from core.ai import extract_json_dict

logger = logging.getLogger(__name__)

# 全局工具注册表：{tool_name: {'fn', 'description', 'params_hint'}}
_REGISTRY = {}

# 意图 → 工具名映射；工具未注册时对应意图自动从协议中隐藏
INTENT_TOOL_MAP = {
    'create': 'activities.create',
    'status_change': 'activities.set_status',
    'query': 'activities.query',
    'get': 'activities.get',
    'update': 'activities.update',
    'delete': 'activities.delete',
    'stats': 'activities.stats',
    'add_expense': 'activities.add_expense',
    'list_expenses': 'activities.list_expenses',
    'split_expense': 'activities.split_expense',
    'move_date': 'activities.move_date',
    'batch_status': 'activities.batch_status',
    'set_budget': 'activities.set_budget',
    'notes_create': 'notes.create',
    'notes_search': 'notes.search',
    'knowledge_search': 'knowledge.search',
    # 把对话结论沉淀成文章（正文由模型自己根据当前会话整理，服务端只负责落库）
    'knowledge_create': 'knowledge.create',
    'set_reminder': 'reminders.set_reminder',
    'list_reminders': 'reminders.list_reminders',
    'complete_reminder': 'reminders.complete',
    'generate_report': 'reports.generate',
    'memory_search': 'memory.search',
}


class ToolError(Exception):
    """工具希望向用户暴露的可读错误（编排器转为友好回复）"""


class CandidateToolError(ToolError):
    """目标不唯一时携带候选列表，编排器渲染候选卡片供用户辨认"""

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


def agent_tool(name, description, params_hint='', apply_fn=None):
    """注册一个 Agent 工具。fn 签名：fn(user, params: dict) -> dict

    返回约定：{'reply': 自然语言, 'card': 卡片类型, 'activity_ids': [...],
               'card_data': 卡片快照数据, 'list_url': 列表页链接, 'changed': 是否写操作,
               'action': 待确认动作 {'tool', 'params'}（两步确认流）}
    apply_fn：两步确认流中确认后的实际执行函数，签名同 fn。
    """
    def deco(fn):
        _REGISTRY[name] = {'fn': fn, 'description': description,
                           'params_hint': params_hint, 'apply': apply_fn}
        return fn
    return deco


def get_tool(name):
    return _REGISTRY.get(name)


def build_protocol_prompt(today=None):
    """动态生成对话首帧意图协议指令"""
    today = today or timezone.localdate()
    lines = []
    for intent, tool_name in INTENT_TOOL_MAP.items():
        tool = _REGISTRY.get(tool_name)
        if not tool:
            continue
        line = f'- {intent}：{tool["description"]}'
        if tool['params_hint']:
            line += f'。params：{tool["params_hint"]}'
        lines.append(line)

    return (
        f'你是「{settings.SITE_NAME}」站点的智能助手。今天是 {today.isoformat()}。\n'
        '你有两类本事，不要混淆：\n'
        '  （1）操作这台站点上的数据（活动/费用/备忘/提醒/知识库/记忆/报告）——这类请求走下面的意图协议；\n'
        '  （2）回答与站点数据无关的通用问题（常识、资讯、攻略、行程建议、“最新/最近”类时效问题）——'
        '这类问题**不要套 JSON**：直接调用你的联网工具（WebSearch / WebFetch）查证，'
        '然后用自然语言回答，结尾列出你实际参考的链接；没查到就直说没查到，不要用记忆硬编。\n\n'
        '对第（1）类消息，判断意图并只输出一个 JSON 对象（不要解释、不要 markdown 代码块）：\n'
        '{"intent": "<意图>", "params": {...}, "reply": "<给用户看的自然语言>"}\n\n'
        '可用意图：\n' + '\n'.join(lines) + '\n'
        '- chitchat：寒暄、感谢、以及不需要外部信息的开放闲聊，只需 reply。\n'
        '- ask：需要外部/最新信息才能答的问题。优先按第（2）类直接联网回答；'
        '若你已查好、只回填给系统展示，才输出 {"intent":"ask","reply":"<完整答案>"}。\n\n'
        '规则：\n'
        '1. 相对日期（明天/下周五/月底等）一律换算为 YYYY-MM-DD 绝对日期，未写年份用当年。\n'
        '2. 写操作意图（create / status_change）的 reply 中不要宣布已执行完成，系统会代为执行。\n'
        '3. 无法识别的字段不要写入 params，禁止编造数据。\n'
        '4. status 取值：planned（计划）/ in_progress（进行中）/ done（已完成）/ cancelled（已取消）。\n'
        '5. 当你从用户的消息中了解到值得长期记住的信息（偏好、目标、个人事实、关系等），'
        '在 JSON 中附加 "memory" 字段（可省略）：\n'
        '   "memory": [{"content": "记忆内容", "category": "preference|fact|goal|relationship|habit|other", "importance": 1-10}]\n'
        '   只记录有长期价值的信息，不要记录临时性请求或操作指令。\n'
        '6. 意图归属：“世界/时效/攻略/建议”这类问题**不要用 knowledge_search 交差**，'
        '它只能在用户自己存的文章里检索；本地没命中也不得回一句「没有找到」就结束，'
        '改走联网回答（必要时先自己查，再顺手告诉用户知识库里没有）。\n'
        '7. 当这一轮有自然的下一步（站点能执行、或你能继续查）时，在回复**最后单独一行**写：'
        '下一步：<完整请求1>｜<完整请求2>（最多 3 个，用全角竖线分隔）。\n'
        '   每个选项必须是一句可以直接发给你的完整请求（带对象名与日期，'
        '不要写「你希望我…」这类反问）；没有自然下一步就不要写这一行。\n'
        '8. 消息开头带 [钉选对象] 时，那个活动的现状（日期/状态/预算/已花费/参与者）'
        '已经全在上下文里：问现状、算数（“还剩多少”）、列参与者都**直接自然语言回答**，'
        '不要为了确认而调 get/query（那等于没读上下文）；只有要改数据或查别的活动才走协议。'
    )


def extract_intent(reply_text):
    """从 AI 回复中提取意图 JSON（兼容前后有说明文字的情况），失败返回 None"""
    data = extract_json_dict(reply_text)
    if not data or not data.get('intent'):
        return None
    return data


# 「下一步」行：回复末尾的一行可点追问选项。
# 为什么不用 JSON 字段（比如 follow_ups）：通用问答走的是「非 JSON 透传」分支，
# 那里根本没有 JSON 可挂字段，而那恰恰是最需要下一步的长回答（查完金价想要行程）。
# 一行文本两条路径都能带，而且模型本来就擅长遵守“最后一行写 X”这种约定。
_FOLLOW_UP_LINE = re.compile(r'\n+(?:【下一步】|\[下一步\]|下一步[：:])\s*([^\n]+?)(?:】|\])?\s*$')
_FOLLOW_UP_SPLIT = re.compile(r'[｜|、;；]')
FOLLOW_UP_MAX = 3
FOLLOW_UP_ITEM_MAX = 40


def extract_follow_ups(text):
    """拆出回复末尾的「下一步：A｜B｜C」→ (去掉该行的正文, [选项])

    认不出来就原样返回：宁可不显示 chips，也不能把用户正文吃掉。
    判据只认「下一步：」与「【下一步】/ [下一步]」两种显式标记（都必须另起一行）：
    正文里一句「下一步是订机票」不能被当成标记剥掉。
    单条选项过长（模型把解释写成了选项）直接丢弃而不是截断，截断后的句子发回去意思就变了。
    """
    text = text or ''
    m = _FOLLOW_UP_LINE.search(text)
    if not m:
        return text, []
    items = []
    for raw in _FOLLOW_UP_SPLIT.split(m.group(1)):
        item = raw.strip().strip('。.').strip()
        if item and len(item) <= FOLLOW_UP_ITEM_MAX:
            items.append(item)
    items = items[:FOLLOW_UP_MAX]
    if not items:
        return text, []
    return text[:m.start()].rstrip(), items


class ChatOrchestrator:
    """解析 AI 意图回复 → 分发工具 → 返回 (content, payload, changed)

    process() 只做一件事：在 _dispatch() 的结果上统一剥「下一步」行。
    放在这一层而不是各 return 点：透传分支、工具成功、ToolError 提示都要能拿到 chips，
    写五遍就是五个会漏改的地方。
    """

    def process(self, user, ai_text):
        content, payload, changed = self._dispatch(user, ai_text)
        content, items = extract_follow_ups(content)
        if not items:
            # 工具路径下工具的 reply 会顶掉模型自己写的 reply（「下一步」行在里面），
            # 真机实测到：钉选后问“还剩多少预算”→ 模型走 get 工具，chips 随之消失。
            # 所以回到模型原文的 reply 里再找一次：模型才是看得到整个上下文的那个。
            data = extract_intent(ai_text)
            if data:
                _, items = extract_follow_ups(str(data.get('reply') or ''))
        if items:
            payload = payload or {'card': '', 'activity_ids': []}
            payload['follow_ups'] = items
        return content, payload, changed

    def _dispatch(self, user, ai_text):
        intent_data = extract_intent(ai_text)
        if not intent_data:
            # 非 JSON 回复：按普通对话透传（兼容未走协议的旧会话）
            return (ai_text or '').strip(), None, False

        intent = str(intent_data.get('intent') or '').strip()
        reply = str(intent_data.get('reply') or '').strip()
        params = intent_data.get('params') or {}
        if not isinstance(params, dict):
            params = {}

        tool_name = INTENT_TOOL_MAP.get(intent)
        # chitchat / ask（未注册工具的意图）：直接把 reply 透给用户，
        # 这就是「通用问答逃生舱」：模型已在自己那一侧用联网工具查完，系统不需要再动作
        if intent == 'chitchat' or not tool_name:
            return reply or (ai_text or '').strip(), None, False

        tool = _REGISTRY.get(tool_name)
        if not tool:
            return reply or '该能力暂未开放。', None, False

        try:
            result = tool['fn'](user, params) or {}
        except CandidateToolError as e:
            logger.info(f'Agent 工具 {tool_name} 需要用户澄清: {e}')
            return (f'{reply}\n\n⚠️ {e}'.strip(),
                    {'card': 'candidates', 'activity_ids': [],
                     'card_data': {'hint': str(e), 'items': e.candidates}}, False)
        except ToolError as e:
            logger.warning(f'Agent 工具 {tool_name} 业务错误: {e}')
            return f'{reply}\n\n⚠️ {e}'.strip(), None, False
        except Exception as e:
            logger.error(f'Agent 工具 {tool_name} 执行失败: {e}')
            return reply or '操作失败，请稍后重试。', None, False

        payload = None
        if result.get('card'):
            payload = {
                'card': result['card'],
                'activity_ids': result.get('activity_ids', []),
            }
            if 'card_data' in result:
                payload['card_data'] = result['card_data']
            if 'list_url' in result:
                payload['list_url'] = result['list_url']
            if 'action' in result:
                # 待确认动作：token 由消息落库后回填（令牌含 message_id）
                payload['action'] = result['action']
        if result.get('created'):
            payload = payload or {'card': '', 'activity_ids': []}
            payload['created_activity_ids'] = result.get('activity_ids', [])

        # ── AI 主动提取记忆（JSON 中的 memory 字段） ──
        memory_list = intent_data.get('memory')
        if memory_list and isinstance(memory_list, list):
            try:
                from memory.services import save_ai_extracted_memories
                save_ai_extracted_memories(user, memory_list)
            except Exception as e:
                logger.warning(f'AI 记忆存储失败: {e}')

        return result.get('reply') or reply, payload, bool(result.get('changed'))


orchestrator = ChatOrchestrator()


def make_action_token(user, message_id, action_key):
    """生成动作确认令牌（HMAC，防篡改），P1 确认流使用"""
    msg = f'{user.id}:{message_id}:{action_key}'.encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, sha256).hexdigest()[:32]
