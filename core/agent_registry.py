"""Agent 工具注册表与对话编排

- 工具注册：各 app 在自己的 agent_tools.py 中用 @agent_tool 装饰器注册，
  core.apps.ready() 自动发现，新增能力无需改动核心对话逻辑
- 意图协议：首帧指令 prompt 由注册表 + INTENT_TOOL_MAP 动态生成；
  AI 回复解析为 {"intent", "params", "reply"} JSON 后分发执行
- 两类消息分流：操作站点数据→意图协议 JSON；通用问答/需要外部信息→模型自己
  调云端联网工具（WebSearch/WebFetch）后以自然语言回，由本编排器的“非 JSON 透传”分支接手
- 容错约定：任何环节失败都降级为普通文本回复，绝不阻断对话。
  但「降级成文本」不等于「把内部协议原文当文本发出去」——见 PROTOCOL_TRUNCATED_NOTE
"""
import hmac
import json
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
    # 费用专项统计：任意时间区间 + 按类别/活动维度汇总（与 stats 的活动概况互补）
    'expense_stats': 'activities.expense_stats',
    'add_expense': 'activities.add_expense',
    'list_expenses': 'activities.list_expenses',
    'split_expense': 'activities.split_expense',
    'move_date': 'activities.move_date',
    'batch_status': 'activities.batch_status',
    'notes_create': 'notes.create',
    'notes_search': 'notes.search',
    'knowledge_search': 'knowledge.search',
    # 把对话结论沉淀成文章（正文由模型自己根据当前会话整理，服务端只负责落库）
    'knowledge_create': 'knowledge.create',
    # 修订已有文章（用户点名「更新/补充《XX》」时用；目标不唯一出候选卡）
    'knowledge_update': 'knowledge.update',
    'generate_report': 'reports.generate',
    'memory_search': 'memory.search',
}

# ── 协议回复被长度上限截断的兜底 ──────────────────────────────────────────────
# 云端平台对单条 assistant 回复有长度上限（实测 2026-09-07 线上：模型想把整篇
# 文章正文塞进 params.content 重抄一遍，回复停在 4075 字符处，花括号只剩 1 个右括号）。
# 残缺的 JSON 解析不出来，于是按旧逻辑会被当成「普通对话」原文透传，界面上出现一整坨
# {"intent":"knowledge_create",...}，而工具其实根本没执行 —— 用户看到一屏乱码，
# 只会以为“要么是没反应，要么是成功了”，永远不会知道这一步什么都没做。
# 文案必须解释「为什么」并给出可操作的下一步，光说「操作失败」等于没说。
PROTOCOL_TRUNCATED_NOTE = (
    '这一步没有执行成功：我这条回复太长，写到一半被截断了，所以指令没收尾，'
    '我也没有替你做任何改动。\n\n'
    '上面那份内容我这边还留着，你再说一次要做什么就行（比如「存进知识库」），'
    '不用让我重写一遍。'
)

# 工具执行炸了（非业务异常）时的兜底文案。也是会落库成 assistant 消息的占位话，
# 「上一条回复」取引用时要认得它——见 chat/views.py 的 _referenceable_reply。
TOOL_FAILURE_REPLY = '操作失败，请稍后重试。'

# 意图键的位置不固定，但协议要求 JSON 以 { 开头，所以只从前缀判协议、意图名单独抠
_PROTOCOL_INTENT = re.compile(r'"intent"\s*:\s*"([\w.]+)"')


def looks_like_protocol(text):
    """这段 AI 回复是不是协议 JSON（哪怕已经残缺到解析不出来）

    判据故意保守：必须以 { 开头且带 "intent" 字样。自然语言回答不会以 { 开头，
    所以不会把「联网问答逃生舱」的正常回复误杀成失败。
    """
    t = (text or '').strip()
    return t.startswith('{') and '"intent"' in t


_PARAMS_KEY = re.compile(r'"params"\s*:\s*\{')
_REPLY_KEY = re.compile(r'"reply"\s*:\s*"')


def _decode_json_string(raw):
    """解码（可能缺了收尾引号的）JSON 字符串内容，解不出来返回空串

    断在转义序列中途（末尾剩一个孤零零的反斜杠）时先去掉它再试一次：
    留着它 json.loads 必炸，而它本来也没表达任何内容。
    """
    for candidate in (raw, raw.rstrip('\\')):
        try:
            return json.loads('"' + candidate + '"')
        except Exception:
            continue
    return ''


def _read_reply(text, start):
    """从 params 收口之后把 reply 读出来 —— 闭合和没闭合都读

    没闭合是线上实测的又一种形状：模型把要给用户看的话写完了，只是漏了收尾的
    ``"}``（stop_reason=end_turn、is_error=false、265 字符，根本没碰长度上限）。
    文字是完整的，报「这一步没有执行成功」等于把一句说完了的回复判成失败。
    """
    key = _REPLY_KEY.search(text, start)
    if not key:
        return ''
    chars = []
    esc = False
    for ch in text[key.end():]:
        if esc:
            chars.append(ch)
            esc = False
        elif ch == '\\':
            chars.append(ch)
            esc = True
        elif ch == '"':
            break                     # 字符串自己收尾了，后面最多是漏掉的右括号
        else:
            chars.append(ch)
    return _decode_json_string(''.join(chars))


def salvage_partial_protocol(text):
    """从残缺的协议 JSON 里抢救 ``{'intent','params','reply'}``，救不了返回 None

    完整串解析不出来时有两种完全相反的残缺，只有前一种不能执行：

    - 正文没抄完（重抄撞长度上限）：截断发生在 params 内部，这里扫不到闭合 → None；
      执行它等于拿半篇内容落库。
    - params 已闭合：动作数据是齐的，坏的只是给模型自己看的文本（reply 写到半路
      就停，或者干脆只是漏了收尾的 ``"}``）→ 按可解析部分执行，并把已经写出来的
      reply 文字带回去（见 _read_reply）。上游抖动不该算在用户头上。
    """
    m = _PROTOCOL_INTENT.search(text or '')
    if not m:
        return None
    key = _PARAMS_KEY.search(text)
    if not key:
        return None
    start = key.end() - 1          # 指向那个 {
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    params = json.loads(text[start:i + 1])
                except Exception:
                    return None
                if not isinstance(params, dict):
                    return None
                return {'intent': m.group(1), 'params': params,
                        'reply': _read_reply(text, i + 1)}
    return None                    # params 从未闭合 → 不救


# ── params 引用展开：让模型不必重抄长正文 ────────────────────────────────────
# 只接受「整个字段值恰好等于标记」，不做子串替换 —— 否则正文里真出现 $LAST_REPLY
# 字样时会被误伤。
_REF_TOKEN = re.compile(r'^\$(LAST_REPLY|LAST_USER)$')
REF_LABELS = {'LAST_REPLY': '你上一条回复的正文', 'LAST_USER': '用户上一条消息'}

# 存量会话的「协议补发」通道：首帧只在建对话时下发一次（chat/views.py 的
# create_conversation），所以协议后来新增的规则永远到不了老对话。线上二次故障：
# 2026-09-07 对话 15（09-06 建的 session）里模型依旧把整篇正文重抄进
# params.content，4031 字符处断掉 —— 修复对存量会话无效。
# 因此把规则 10 拼在本轮发给平台的正文前面递一次（走 _build_ai_content，与
# [钉选对象] 同一条通道：不额外发帧、不撞 409，也不污染用户消息历史）。
PROTOCOL_REF_REMINDER = (
    '[协议补充] params 里要放本轮已经出现过的长内容时，字段值只写引用标记 '
    '"$LAST_REPLY"（上一条你给用户看的正文）或 "$LAST_USER"（用户上一条消息），'
    '系统会自行展开成原文。把正文重抄进 JSON 会撞上单条回复的长度上限，'
    '整条指令会被截断而静默失败。\n\n'
)
# 上一条回复不够长就没有「可抄的东西」，不必每轮注噪声；阈值远低于会撞上限的长度
REF_REMINDER_MIN_CHARS = 800


def resolve_params_refs(params, refs):
    """把 params 里值为 "$LAST_REPLY" / "$LAST_USER" 的字段换成实际文本

    返回 (展开后的 params, 错误文案或 None)。引用取不到内容时报错而不是把标记
    原样交给工具：那十个字符被当成正文存进知识库，比失败更糟。
    """
    refs = refs or {}
    out, error = {}, None

    def _one(value):
        nonlocal error
        if not isinstance(value, str):
            return value
        m = _REF_TOKEN.match(value.strip())
        if not m:
            return value
        key = m.group(1)
        text = str(refs.get(key) or '').strip()
        if not text:
            error = f'没能取到{REF_LABELS[key]}（这一轮没有可引用的内容），请直接说出要处理的内容'
            return value
        return text

    for key, value in (params or {}).items():
        if isinstance(value, list):
            out[key] = [_one(item) for item in value]
        else:
            out[key] = _one(value)
    return out, error


# 「第 N 个」序号解析：用户面对候选卡习惯打字回答「第一个」而不是点按钮。
_CN_PICK_NUM = {'一': 1, '两': 2, '二': 2, '三': 3, '四': 4, '五': 5,
               '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def parse_pick_index(value):
    """把用户/模型给的序号解析成 1-based int，解析不出返回 None

    支持：1、"2"、"第一个"、"第2个"、"第二个"。候选最多 5 个，
    十位数及以上的中文序号不支援（返回 None 走原匹配逻辑）。
    """
    if value is None:
        return None
    m = re.search(r'第?\s*([0-9０-９一二两三四五六七八九十]+)\s*个?', str(value))
    if not m:
        return None
    token = m.group(1)
    if token.isdigit():
        return int(token)
    if token in _CN_PICK_NUM:
        return _CN_PICK_NUM[token]
    return None


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
    params_hint：静态字符串，或 () -> str 的 callable——内容随数据库配置变化的
    工具（如费用标签建议来自 core.Tag 表，admin 改完即生效）用它，
    每帧生成协议时实时求值，避免进程常驻后 hint 与数据脱节。
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
        hint = tool['params_hint']
        if callable(hint):
            # 动态 hint 求值失败不能炸掉整个协议 prompt，降级为无参数说明
            try:
                hint = hint()
            except Exception:
                logger.warning('动态 params_hint 求值失败: tool=%s', tool_name, exc_info=True)
                hint = ''
        if hint:
            line += f'。params：{hint}'
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
        '不要为了确认而调 get/query（那等于没读上下文）；只有要改数据或查别的活动才走协议。\n'
        '9. 当你需要回忆用户之前告诉过你的信息（偏好、经历、关系、目标等）时，'
        '调用 memory_search 工具查询；不要凭印象编造。首帧已注入部分记忆，'
        '但若话题涉及更早或更具体的内容，主动搜索。\n'
        '10. params 里要放**本轮对话里已经出现过的长内容**（你上一条回复的正文、用户刚贴的一整段）时，'
        '不要重新抄写正文：把该字段的值写成引用标记 "$LAST_REPLY"（引用你上一条给用户看的回复）'
        '或 "$LAST_USER"（引用用户上一条消息），系统会自行替换成原文。'
        '重抄长正文会撞上单条回复的长度上限，整条指令会被截断而静默失败。\n'
        '11. 候选澄清：上一轮系统提示了候选列表、用户回答了「第几个」（第一个/第2个/2 等）时，'
        '在 params 里加 "pick": N（1 起算的序号，指上一轮候选列表里的第 N 项），'
        'target 仍照填原名；系统会按序号直达目标，不要重新查询或让用户再描述一遍。'
        '没有候选澄清场景时禁止带 pick 字段。\n'
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


def _pick_from_text(text):
    """从自然语言里提取「第 N 个」序号，带否定语境防呆（「不要第一个」不触发）

    解析不出返回 None。用户/模型都可能说序号：用户「第一个，计划中的」，
    模型复述「收到，按这个改：第一个…」。
    """
    if not text:
        return None
    m = re.search(r'第?\s*([0-9０-９一二两三四五六七八九十]+)\s*个?', str(text))
    if not m:
        return None
    if re.search(r'(不|别|除了|莫|勿)', str(text)[max(0, m.start() - 3):m.start()]):
        return None
    return parse_pick_index(m.group(1))


class ChatOrchestrator:
    """解析 AI 意图回复 → 分发工具 → 返回 (content, payload, changed)

    process() 只做一件事：在 _dispatch() 的结果上统一剥「下一步」行。
    放在这一层而不是各 return 点：透传分支、工具成功、ToolError 提示都要能拿到 chips，
    写五遍就是五个会漏改的地方。
    """

    def process(self, user, ai_text, refs=None, user_text=None, pick_context=None):
        content, payload, changed = self._dispatch(user, ai_text, refs,
                                                   user_text=user_text,
                                                   pick_context=pick_context)
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

    @staticmethod
    def _candidate_payload(reply, error, tool_name, params):
        """候选澄清卡的消息 payload；pending_action 快照供「选它」按钮服务端重放"""
        return (f'{reply}\n\n⚠️ {error}'.strip(),
                {'card': 'candidates', 'activity_ids': [],
                 'card_data': {'hint': str(error), 'items': error.candidates,
                               'pending_action': {'tool': tool_name,
                                                  'params': params}}}, False)

    def _dispatch(self, user, ai_text, refs=None, user_text=None, pick_context=None):
        intent_data = extract_intent(ai_text)
        if not intent_data:
            # 「不是合法 JSON」有两种完全相反的情况，必须分开：
            # (a) 模型本来就在用自然语言回答（联网问答逃生舱）→ 透传是对的；
            # (b) 模型按协议输出了 JSON，但回复被平台长度上限截断在半路 → 解析不出来。
            # 旧代码只假设了 (a)，于是 (b) 的协议原文直接落到用户脸上（见 PROTOCOL_TRUNCATED_NOTE）。
            if looks_like_protocol(ai_text):
                m = _PROTOCOL_INTENT.search(ai_text)
                name = m.group(1) if m else '?'
                salvaged = salvage_partial_protocol(ai_text)
                tool_name = INTENT_TOOL_MAP.get((salvaged or {}).get('intent', ''))
                has_tool = bool(tool_name and _REGISTRY.get(tool_name))
                if has_tool or (salvaged or {}).get('reply'):
                    # 有工具可执行，或者至少取出了给用户看的话（chitchat / ask：没工具，
                    # 但回复文字本身已经写出来了）。两者都不是时走下去会把协议原文当正文透给用户。
                    logger.warning(
                        f'协议 JSON 残缺但已捞到可用部分，继续执行 '
                        f'intent={name} 工具={"有" if has_tool else "无"} '
                        f'长度={len(ai_text or "")}')
                    intent_data = salvaged
                else:
                    logger.warning(
                        f'协议 JSON 残缺（正文可能被抄进 params 后截断），未执行工具 '
                        f'intent={name} 长度={len(ai_text or "")}')
                    return PROTOCOL_TRUNCATED_NOTE, None, False
            # 情况 (a)：按普通对话透传（兼容未走协议的旧会话）
            if not intent_data:
                return (ai_text or '').strip(), None, False

        intent = str(intent_data.get('intent') or '').strip()
        reply = str(intent_data.get('reply') or '').strip()
        params = intent_data.get('params') or {}
        if not isinstance(params, dict):
            params = {}
        # 引用标记展开（协议规则 10）：模型写 "$LAST_REPLY" 而不必重抄正文
        params, ref_error = resolve_params_refs(params, refs)
        if ref_error:
            logger.warning(f'协议引用未能展开: {ref_error}')
            return f'{reply}\n\n⚠️ {ref_error}'.strip(), None, False

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
            # 兜底：用户面对候选卡习惯打字回「第一个」而不是点按钮。存量 session
            # 的首帧协议没有规则 11，模型不会带 pick 字段，纯文字回复会每轮重新
            # 撞候选错误形成死循环（线上实测：同一句预览连出 5 次）。这里服务端
            # 直接从用户原文（其次模型回复）解析序号，对上一轮候选列表重放工具。
            # pick_context 由调用方（chat.views）从最近一条候选卡消息取来传入，
            # core 不反向依赖业务 app。
            index = _pick_from_text(user_text) or _pick_from_text(ai_text)
            pick_tool = (pick_context or {}).get('tool') or ''
            pick_items = (pick_context or {}).get('items') or []
            same_family = pick_tool.split('.')[0] == tool_name.split('.')[0]
            if index is not None and same_family and 1 <= index <= len(pick_items):
                try:
                    result = tool['fn'](user,
                                        {**params,
                                         'target_id': pick_items[index - 1]['id']}) or {}
                    logger.info(f'候选序号直达: 第 {index} 个 → '
                                f'{pick_items[index - 1].get("name", "")}')
                except CandidateToolError as e2:
                    logger.info(f'候选序号重放仍需澄清: {e2}')
                    return self._candidate_payload(reply, e2, tool_name, params)
                except ToolError as e2:
                    logger.warning(f'候选序号重放业务错误: {e2}')
                    return f'{reply}\n\n⚠️ {e2}'.strip(), None, False
                except Exception as e2:
                    logger.error(f'候选序号重放执行失败: {e2}')
                    return reply or TOOL_FAILURE_REPLY, None, False
            else:
                # 序号解析不出 / 无候选上下文 / 序号越界：照常渲染候选卡引导用户
                return self._candidate_payload(reply, e, tool_name, params)
        except ToolError as e:
            logger.warning(f'Agent 工具 {tool_name} 业务错误: {e}')
            return f'{reply}\n\n⚠️ {e}'.strip(), None, False
        except Exception as e:
            logger.error(f'Agent 工具 {tool_name} 执行失败: {e}')
            return reply or TOOL_FAILURE_REPLY, None, False

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
