"""Agent 工具注册表与对话编排

- 工具注册：各 app 在自己的 agent_tools.py 中用 @agent_tool 装饰器注册，
  core.apps.ready() 自动发现，新增能力无需改动核心对话逻辑
- 意图协议：首帧指令 prompt 由注册表 + INTENT_TOOL_MAP 动态生成；
  AI 回复解析为 {"intent", "params", "reply"} JSON 后分发执行
- 容错约定：任何环节失败都降级为普通文本回复，绝不阻断对话
"""
import hmac
import json
import logging
import re
from hashlib import sha256

from django.conf import settings
from django.utils import timezone

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
    'set_reminder': 'reminders.set_reminder',
    'list_reminders': 'reminders.list_reminders',
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
        f'你是「个人助手」站点的活动管理助手。今天是 {today.isoformat()}。\n'
        '对用户的每条消息，判断意图并只输出一个 JSON 对象（不要解释、不要 markdown 代码块）：\n'
        '{"intent": "<意图>", "params": {...}, "reply": "<给用户看的自然语言>"}\n\n'
        '可用意图：\n' + '\n'.join(lines) + '\n'
        '- chitchat：普通闲聊或无法归类的消息，只需 reply。\n\n'
        '规则：\n'
        '1. 相对日期（明天/下周五/月底等）一律换算为 YYYY-MM-DD 绝对日期，未写年份用当年。\n'
        '2. 写操作意图（create / status_change）的 reply 中不要宣布已执行完成，系统会代为执行。\n'
        '3. 无法识别的字段不要写入 params，禁止编造数据。\n'
        '4. status 取值：planned（计划）/ in_progress（进行中）/ done（已完成）/ cancelled（已取消）。\n'
        '5. 当你从用户的消息中了解到值得长期记住的信息（偏好、目标、个人事实、关系等），'
        '在 JSON 中附加 "memory" 字段（可省略）：\n'
        '   "memory": [{"content": "记忆内容", "category": "preference|fact|goal|relationship|habit|other", "importance": 1-10}]\n'
        '   只记录有长期价值的信息，不要记录临时性请求或操作指令。'
    )


def extract_intent(reply_text):
    """从 AI 回复中提取意图 JSON（兼容前后有说明文字的情况），失败返回 None"""
    m = re.search(r'\{.*\}', reply_text or '', re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get('intent'):
        return None
    return data


class ChatOrchestrator:
    """解析 AI 意图回复 → 分发工具 → 返回 (content, payload, changed)"""

    def process(self, user, ai_text):
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
