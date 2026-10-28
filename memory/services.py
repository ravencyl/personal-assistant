"""记忆服务层：检索 / 注入格式化 / 规则兜底提取

架构约定：
- retrieve_memories：按重要度 + 时效性检索，可选关键词匹配
- format_memory_for_injection：格式化为注入 AI 的文本上下文
- extract_memories_from_text：对用户消息做模式匹配，兜底提取记忆
- 所有函数失败仅 logger.warning，不抛异常（容错铁律）
"""
import logging
import re

from django.db import models
from django.db.models import F
from django.utils import timezone

from knowledge.utils import tokenize

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

# 纠正/方法类句式 → type=procedural（PRD §3.1：把「用户纠正过我」单独成层，
# 否则该遵守的规则会被当成可选背景知识稀释掉）。category 兜 'other'，type 才是层。
CORRECTION_PATTERNS = [
    # (正则, importance)。「别」与「不要」都要接得住（用户实际说法两种都有，
    # 只写「不要」会漏「以后别用 bullet 铺满整页」这类高频纠正），
    # 「下次直接/以后就」这类直陈式也算纠正
    (r'(?:以后|下次|往后)(?:都)?(?:请)?不要([^。，！？,\n]{2,80})', 8),
    (r'(?:以后|下次|往后)(?:都)?(?:请)?别再?([^。，！？,\n]{2,80})', 8),
    (r'(?:以后|下次|往后)(?:都)?(?:要|记得|尽量|直接|就)([^。，！？,\n]{2,80})', 7),
    (r'别再([^。，！？,\n]{2,80})', 8),
    (r'我(?:一般|通常|习惯)(?:都是)?(?:用|按|先)([^。，！？,\n]{2,80})', 6),
]


# ────────────────────────────────────────────────
# 检索 + 注入
# ────────────────────────────────────────────────

# 记忆衰减参数：30 天半衰期，90 天后权重降到原始的 1/8。
# 这个节奏匹配「个人助手」的使用频率——周级别活跃的用户，
# 一个月前的偏好仍然有价值，三个月前的就该让位给新信息了。
_DECAY_HALF_LIFE_DAYS = 30

# access_count 转加分的上限：防止高频访问的低价值记忆抢占位置。
# 每 10 次访问加 1 分，最多补 2 分（即 access_count >= 20 后不再涨）。
_ACCESS_BONUS_CAP = 2.0
_ACCESS_BONUS_DIVISOR = 10


def _memory_score(memory, now):
    """计算单条记忆的动态权重：importance × 时间衰减 + 访问加分

    为什么不在 DB 层算：SQLite 没有 pow/log 函数，写 CASE WHEN 模拟指数衰减
    可读性差且难测试。当前用户记忆量级远不到 50 条，Python 排序开销可忽略；
    等真到几百条时再迁移到 annotate + 数据库表达式。
    """
    days = max(0, (now - memory.updated_at).total_seconds() / 86400)
    decay = 0.5 ** (days / _DECAY_HALF_LIFE_DAYS)
    access_bonus = min(memory.access_count / _ACCESS_BONUS_DIVISOR,
                       _ACCESS_BONUS_CAP)
    return memory.importance * decay + access_bonus


def retrieve_memories(user, query='', limit=10, category=''):
    """检索用户记忆，按动态权重排序（importance × 时间衰减 + 访问加分）

    - query 非空时：先 tokenize 分词，任一词 icontains 命中即入选（多词 OR，
      比「整句 icontains」召回高一个量级——用户提问是自然语言，整句模糊匹配
      几乎必然落空）；分不出词元（全是停用词）时回退整句 icontains
    - category 非空且合法时按类别过滤（memory_search 工具用，注入路径不传）
    - query 为空时：取候选集 Top 50 按 importance DESC，再 Python 排动态权重取 limit
    - 更新命中记忆的 access_count + last_accessed（批量，失败不阻断）

    只取本人记忆（不按 visible_qs 放宽）：注入的是“正在对话的这个人的上下文”，
    与页面浏览类的“超管见全部”是两个口径。
    """
    from .models import Memory

    try:
        qs = (Memory.objects.filter(user=user, consolidated=False,
                                    status__in=['active', 'candidate'])
              .exclude(type='document'))  # 文章走 knowledge.search，不混进记忆检索

        if query and query.strip():
            tokens = tokenize(query)
            if tokens:
                q = models.Q()
                for token in tokens:
                    q |= models.Q(content__icontains=token)
                qs = qs.filter(q)
            else:
                qs = qs.filter(content__icontains=query.strip())

        valid_categories = dict(Memory.CATEGORY_CHOICES).keys()
        if category and category in valid_categories:
            qs = qs.filter(category=category)

        # 取候选集：按原始 importance 取前 50 条，再 Python 排动态权重。
        # 50 是保守上界：当前用户记忆量级远低于此，未来若超了再调大或迁 DB 表达式。
        candidates = list(qs.order_by('-importance', '-updated_at')[:50])

        now = timezone.now()
        candidates.sort(key=lambda m: _memory_score(m, now), reverse=True)
        memories = candidates[:limit]

        # 更新访问计数（批量，失败仅告警）
        if memories:
            try:
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

# ────────────────────────────────────────────────
# 写入门控（ROA PRD §5.2 / §5.2.1 / §9.2 / §9.3.1）
# 原则：少记、快记、记不准就先不记。所有写入路径必须经过 gate_and_write。
# ────────────────────────────────────────────────

# denylist：命中即拒绝入库。可推导的信息（代码/文件全文）不存——用陈旧快照
# 冒充事实比不存更糟；证件/凭证/内网标识不存——公网服务器 + 第三方模型推理，
# 写入即出境。返回值是给审计日志与用户提示看的类别标签。
# 数字类用环视不用 \b：Python 3 的 \b 把中文也算 \w，「身份证号是1101…」里
# 汉字与数字之间不存在词边界，\b 版本整条静默落空（tests.py 实测抓到）。
DENYLIST_PATTERNS = [
    (r'(?<!\d)\d{17}[\dXx](?!\d)', '证件号'),
    (r'(?<!\d)\d{16,19}(?!\d)', '卡号/账号'),
    (r'(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=＝]\s*\S+', '凭证'),
    (r'(?:密码|口令|密钥)\s*[:：是为]?\s*\S{4,}', '凭证'),
    (r'\.(?:loreal|corp|local|internal|intra|lan)\b', '内网域名'),
    (r'(?<![\d.])'
     r'(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}'
     r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)', '内网地址'),
]

# sensitivity=sensitive 的内容类别（PRD §9.2）：默认不写，owner 明确要求记住才写
SENSITIVE_HINTS = [
    '确诊', '病史', '体检报告', '处方', '用药', '服药', '服用', '抑郁', '心理咨询',
    '工资', '月薪', '年薪', '房贷', '车贷', '存款', '负债', '征信',
    '性取向', '出轨', '离婚', '分手', '流产',
]

# confidence 分档（PRD 附录 B）。阈值刻意先松后紧：线上没有独立小模型评估，
# 置信来自云端 Agent 的自我估计，跑一周看候选堆积情况再收紧。
CONF_ACTIVE = 0.55      # ≥ → 直接 active（进常驻注入池）
CONF_CANDIDATE = 0.35   # [candidate, active) → candidate，不进常驻，等冷路径复验

# 用户显式要求记住的标记（敏感内容唯一的写入通行证，PRD §9.2）
EXPLICIT_REMEMBER_RE = re.compile(r'记住|记一下|记下来|帮我记|别忘了')


def check_denylist(text):
    """命中 denylist 返回类别标签，否则 None"""
    for pattern, label in DENYLIST_PATTERNS:
        if re.search(pattern, text or ''):
            return label
    return None


def log_audit(user, action, memory=None, detail=''):
    """记忆操作审计（append-only）。失败仅告警——审计缺失不得反过来弄断记忆写入"""
    try:
        from .models import MemoryAudit
        MemoryAudit.objects.create(
            user=user, memory=memory, action=action,
            detail=(detail or '')[:300],
        )
    except Exception as e:
        logger.warning(f'记忆审计写入失败: {e}')


def assess_sensitivity(text):
    """健康/财务/证件/亲密关系类内容 → sensitive（默认拒绝写入）"""
    return any(hint in (text or '') for hint in SENSITIVE_HINTS)


def _find_similar(user, content, exclude_ids=None):
    """返回内容高度相似的已有记忆（合并计数用），无则 None

    注意这里的 filter(user=user) 不是可见性漏改：注入 / 查重属于「这个人的个人
    上下文」，超级用户也不应把别人的记忆灌进自己的对话；页面浏览类的“超管见全部”
    只适用于视图（memory/views.py 走 visible_qs）。
    exclude_ids：取代链场景要排除被取代的旧条——旧条与新条内容必然相近，
    不排除的话新条会被当成「重复」并进旧条，取代永远发生不了。
    """
    from core.utils import char_overlap_ratio
    from .models import Memory

    if len(content) < 3:
        return None
    qs = Memory.objects.filter(
        user=user, consolidated=False, status__in=['active', 'candidate'],
    ).exclude(type='document')  # 文章不参与记忆查重/合并（长文会假阳性命中）
    if exclude_ids:
        qs = qs.exclude(id__in=exclude_ids)
    existing = qs.order_by('-importance', '-updated_at')[:100]
    for memory in existing:
        if len(memory.content) < 3:
            continue
        if char_overlap_ratio(content, memory.content, mode='contains') > 0.8:
            return memory
    return None


def _is_similar_content(user, content, threshold=0.8):
    """兼容旧签名的查重判断（阈值参数保留，合并逻辑统一走 _find_similar）"""
    from core.utils import char_overlap_ratio

    memory = _find_similar(user, content)
    if memory is None:
        return False
    return char_overlap_ratio(content, memory.content, mode='contains') > threshold


def gate_and_write(user, candidate, source_message=None, user_text='',
                   exclude_ids=None, audit=True):
    """统一写入口：denylist → 敏感闸门 → 近似合并 → confidence 分档

    candidate: {'content', 'category', 'importance', 'type', 'hook',
                'confidence', 'sensitivity'}（后四个可省）
    user_text: 触发本轮写入的用户原话——敏感内容只有用户显式说「记住」才放行。
    返回 (action, memory_or_None, note)：
      action ∈ written / merged / candidate / dropped / denied / blocked
      note 为给用户的提示（denied/blocked 时非空，其余为空）。
    失败仅 logger.warning，不抛异常（容错铁律）。
    """
    from .models import Memory

    content = str(candidate.get('content', '')).strip()
    if not content or len(content) < 2 or len(content) > 500:
        return 'dropped', None, ''

    label = check_denylist(content)
    if label:
        logger.warning(f'记忆写入被 denylist 拦截（{label}）: {content[:60]}')
        return ('denied', None,
                f'有一段内容涉及{label}，按隐私规则我不会把它存进记忆。')

    memory_type = str(candidate.get('type') or 'semantic').strip()
    if memory_type == 'document':
        # 文章不允许从记忆门控进：门控按 500 字短条口径处理，长文会被截成碎片。
        # 文章只经 knowledge.create / 表单创建，这里强制回落语义条。
        memory_type = 'semantic'
    if memory_type not in dict(Memory.TYPE_CHOICES):
        memory_type = 'semantic'

    explicit = bool(EXPLICIT_REMEMBER_RE.search(user_text or ''))
    is_sensitive = (str(candidate.get('sensitivity') or '').strip() == 'sensitive'
                    or assess_sensitivity(content))
    if is_sensitive and not explicit:
        logger.warning(f'敏感记忆未获用户显式确认，已拦下: {content[:60]}')
        return ('blocked', None,
                '有一条涉及个人敏感信息的记忆我没有保存；'
                '确实要记的话请直接说「记住：……」。')

    similar = _find_similar(user, content, exclude_ids=exclude_ids)
    if similar is not None:
        # 近似命中从「跳过」改为「合并计数」（PRD §5.2）：被反复确认的事实
        # 应该变强，而不是被当成重复丢掉。
        try:
            Memory.objects.filter(id=similar.id).update(
                access_count=F('access_count') + 1,
                last_verified=timezone.now(),
            )
        except Exception as e:
            logger.warning(f'记忆合并计数失败: {e}')
        if audit:
            log_audit(user, 'merge', memory=similar, detail=content)
        return 'merged', similar, ''

    try:
        importance = candidate.get('importance', 5)
        try:
            importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            importance = 5

        category = str(candidate.get('category') or 'other').strip()
        if category not in dict(Memory.CATEGORY_CHOICES):
            category = 'other'

        confidence = candidate.get('confidence')
        try:
            confidence = None if confidence in (None, '') else max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
        # 未给置信度时按重要度折算（AI 路径两者同源）；规则兜底路径固定 0.8
        if confidence is None:
            confidence = round(importance / 10, 2)

        hook = str(candidate.get('hook') or '').strip()[:150]
        salience = str(candidate.get('salience') or 'normal').strip()
        if salience not in ('high', 'normal', 'low'):
            salience = 'normal'

        if confidence >= CONF_ACTIVE:
            action = 'written'
        elif confidence >= CONF_CANDIDATE:
            action = 'candidate'
        else:
            return 'dropped', None, ''
        status = 'active' if action == 'written' else 'candidate'

        memory = Memory.objects.create(
            user=user,
            content=content,
            hook=hook,
            type=memory_type,
            category=category,
            importance=importance,
            confidence=confidence,
            salience=salience,
            sensitivity='sensitive' if is_sensitive else 'normal',
            status=status,
            last_verified=timezone.now(),
            source_message=source_message,
        )
        logger.info(f'记忆写入 ({action}) type={memory_type} status={status} '
                    f'conf={confidence}: {content[:60]}')
        if audit:
            log_audit(user, 'write' if action == 'written' else 'candidate',
                      memory=memory, detail=content)
        return action, memory, ''
    except Exception as e:
        logger.warning(f'记忆写入失败: {e}')
        return 'dropped', None, ''


def supersede_memory(user, old, candidate, source_message=None, user_text=''):
    """取代链（PRD §5.4）：旧事实被新事实推翻时，取代而非并存或覆盖

    - 新条走 gate_and_write（exclude_ids 排除旧条：新旧内容必然相近，
      不排除新条会被合并回旧条，取代永远发生不了）
    - 旧条置 invalid_at + superseded_by + status=superseded（保留推理依据，
      不参与召回，可回溯「什么时候认为过什么」）
    - 新条与旧条内容一致（只是重述）时视为细化，走合并计数不取代
    返回 (action, memory, note)，action ∈ written / merged / dropped / denied / blocked
    """
    from django.utils import timezone as tz
    from .models import Memory

    new_content = str(candidate.get('content') or '').strip()
    if not new_content:
        return 'dropped', None, ''
    if new_content == old.content:
        # 完全一致 = 重述确认，走合并计数不取代。近似但不等（如「周三→周二」）
        # 的差异恰恰是用户要更正的东西，必须走取代——字符重合度高不等于没变更
        Memory.objects.filter(id=old.id).update(
            access_count=F('access_count') + 1, last_verified=tz.now())
        log_audit(user, 'merge', memory=old, detail=new_content)
        return 'merged', old, ''

    action, memory, note = gate_and_write(
        user, candidate, source_message=source_message, user_text=user_text,
        exclude_ids=[old.id])
    if action in ('written', 'candidate') and memory:
        old.status = 'superseded'
        old.invalid_at = tz.now().date()
        old.superseded_by = memory
        old.save(update_fields=['status', 'invalid_at', 'superseded_by', 'updated_at'])
        log_audit(user, 'supersede', memory=memory,
                  detail=f'「{old.content[:80]}」→「{memory.content[:80]}」')
        logger.info(f'记忆取代: #{old.id} → #{memory.id}')
    return action, memory, note


def extract_memories_from_text(user, text, source_message=None):
    """对用户消息做模式匹配，返回新创建的 Memory 列表（规则兜底路径）

    - 命中即高置信（规则兜底本身是确定性正则，confidence 固定 0.8 → active）
    - 纠正/方法句式单独成 procedural 层
    - 相似命中走合并计数；denylist / 敏感闸门同样生效
    - 失败仅 logger.warning，不抛异常
    """
    from .models import Memory

    if not text or not text.strip():
        return []

    created = []
    try:
        for pattern, category, importance in EXTRACTION_PATTERNS:
            for match in re.findall(pattern, text):
                _rule_write(user, {'content': match, 'category': category,
                                   'importance': importance},
                            source_message, created)

        for pattern, importance in CORRECTION_PATTERNS:
            for match in re.findall(pattern, text):
                _rule_write(user, {'content': match.strip(), 'category': 'other',
                                   'importance': importance, 'type': 'procedural'},
                            source_message, created)

    except Exception as e:
        logger.warning(f'规则记忆提取失败: {e}')

    return created


def _rule_write(user, candidate, source_message, created):
    """规则兜底路径的单条写入：清洗 + 门控 + 收集新建条目"""
    content = str(candidate.get('content', '')).strip()
    # 过滤太短或太长的匹配
    if len(content) < 2 or len(content) > 200:
        return
    candidate['content'] = content
    candidate['confidence'] = 0.8
    action, memory, _note = gate_and_write(user, candidate,
                                           source_message=source_message,
                                           user_text=source_message.content
                                           if source_message else '')
    if action in ('written', 'candidate') and memory:
        created.append(memory)
        logger.info(f'规则提取记忆: ({candidate.get("category")}) {content}')


# ────────────────────────────────────────────────
# AI 提取（由 orchestrator 调用）
# ────────────────────────────────────────────────

def save_ai_extracted_memories_ex(user, memory_list, source_message=None,
                                  user_text=''):
    """存储 AI 主动提取的记忆，返回 (created, blocked_notes)

    blocked_notes 是给用户的可见提示（denylist/敏感闸门拦下的条目）——
    闸门必须可见，静默丢弃会让用户以为「它记住了」。
    """
    from .models import Memory

    created, blocked = [], []
    if not memory_list:
        return created, blocked

    try:
        for item in memory_list:
            if not isinstance(item, dict):
                continue
            content = str(item.get('content', '')).strip()
            if not content or len(content) < 2 or len(content) > 500:
                continue
            action, memory, note = gate_and_write(
                user, item, source_message=source_message, user_text=user_text)
            if action in ('written', 'candidate') and memory:
                created.append(memory)
                logger.info(f'AI 提取记忆: ({memory.category}) {content}')
            elif note:
                blocked.append(note)
    except Exception as e:
        logger.warning(f'AI 记忆存储失败: {e}')

    return created, blocked


def save_ai_extracted_memories(user, memory_list, source_message=None):
    """兼容旧签名：只返回新建列表（编排器用 _ex 变体拿拦截明细）"""
    created, _blocked = save_ai_extracted_memories_ex(
        user, memory_list, source_message=source_message)
    return created


# ────────────────────────────────────────────────
# 常驻注入（PRD §4.3 路径一：索引常驻 + procedural 全文）
# ────────────────────────────────────────────────

# 注入字符预算（≈ token 预算的中文换算）：索引行 + procedural 全文共用。
# 超限按动态权重截断，保证注入成本不随记忆总量增长（PRD G5）。
INJECTION_CHAR_BUDGET = 3000
INDEX_LINE_MAX = 90          # 索引行单条上限（hook 缺失时截 content）
PROCEDURAL_FULL_MAX = 12     # procedural 全文条数上限
EPISODE_LINE_MAX = 60        # 情景行单条上限（近 3 条 episodic 的跨会话连续性）
RECENT_EPISODES = 3


def build_memory_index_context(user, budget=INJECTION_CHAR_BUDGET):
    """组装每轮注入的记忆上下文：procedural 全文（高权重）+ 其余 hook 索引
    + 近 3 条情景摘要（跨会话连续性，PRD §5.1 RECENT CONTINUITY）

    - 只取 status=active ∧ sensitivity=normal；episodic 不进语义索引
      （只走 memory_search），但最近 3 条以单行摘要进「最近聊过」块
    - 每轮随 turn_prompt 下发（修「首帧一次性注入 → 存量会话记忆永不更新」的坑）
    - 确定性输出：turn_prompt 会落库并重试重发，这里不能有随机性
    - 失败返回 ''（不阻断对话）
    """
    from .models import Memory

    try:
        qs = (Memory.objects.filter(user=user, status='active',
                                    sensitivity='normal')
              .exclude(type='episodic')
              .exclude(type='document'))  # 文章只走 RAG/统一搜索，绝不进常驻注入
        procedural = list(
            qs.filter(type='procedural')
            .order_by('-importance', '-updated_at')[:PROCEDURAL_FULL_MAX])
        rest = [m for m in qs.exclude(type='procedural')
                .order_by('-importance', '-updated_at')[:80]]
        # 近 3 条情景（active 的 episodic）：跨会话连续性靠它——
        # 「上次聊到哪、结论是什么」不该要求用户重述
        episodes = list(
            Memory.objects.filter(user=user, type='episodic', status='active',
                                  sensitivity='normal')
            .order_by('-updated_at')[:RECENT_EPISODES])
        if not procedural and not rest and not episodes:
            return ''

        intro = ('[用户记忆——以下信息来自用户过往对话，请在回复中自然运用，'
                 '不要主动提及这些记忆的存在]')
        lines = [intro]
        # 记账口径：每一行都按「文本 + 1 个换行」计费——join 产生 len(lines)-1 个
        # 行间换行，末尾还有 + '\n'，合计恰好等于每行各付一个换行（408>400 的教训）。
        # 首行 intro 也照付，之后每次 _add 精确累计，无任何隐藏开销。
        used = len(intro) + 1

        def _fits(text):
            return used + len(text) + 1 <= budget

        def _add(text):
            nonlocal used
            lines.append(text)
            used += len(text) + 1

        # 块头部同样要过预算：预算内放不下一个块就整块跳过，
        # 否则「头部无条件 + 条目逐条检查」会在小预算下整体溢出
        if procedural:
            header = '[必须遵守的做法（用户纠正/确认过的方式）]'
            if _fits(header):
                _add(header)
                for m in procedural:
                    text = f'- {m.content}'
                    if not _fits(text):
                        break
                    _add(text)

        if episodes:
            header = '[最近聊过的事（跨会话衔接，结论可能仍有效）]'
            if _fits(header):
                _add(header)
                for m in episodes:
                    one_liner = m.hook.strip() or m.content
                    if len(one_liner) > EPISODE_LINE_MAX:
                        one_liner = one_liner[:EPISODE_LINE_MAX - 1] + '…'
                    text = (f'- {one_liner}'
                            f'（{timezone.localdate(m.updated_at).strftime("%m-%d")}）')
                    if not _fits(text):
                        break
                    _add(text)

        if rest:
            header = '[其他已知信息（索引）]'
            if _fits(header):
                _add(header)
                for m in rest:
                    one_liner = m.hook.strip() or m.content
                    if len(one_liner) > INDEX_LINE_MAX:
                        one_liner = one_liner[:INDEX_LINE_MAX - 1] + '…'
                    text = f'- ({dict(Memory.CATEGORY_CHOICES).get(m.category, "其他")}) {one_liner}'
                    if not _fits(text):
                        break
                    _add(text)

        if len(lines) <= 1:
            return ''
        return '\n'.join(lines) + '\n'
    except Exception as e:
        logger.warning(f'记忆注入组装失败: {e}')
        return ''


# ────────────────────────────────────────────────
# 归档/回顾的启发式提取
# ────────────────────────────────────────────────

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
    from .models import Memory, MEMORY_CONTENT_MAX_LENGTH

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
        content = (f'对话「{title}」' + '；'.join(parts))[:MEMORY_CONTENT_MAX_LENGTH]

        # 门控统一出口：规则路径确定性高（confidence 0.8 → active），
        # 但 denylist / 敏感闸门照常生效（对话内容也可能带凭证）
        action, memory, _note = gate_and_write(
            conversation.user,
            {'content': content, 'category': 'other', 'type': 'episodic',
             'importance': 4, 'confidence': 0.8},
            source_message=last_answer)
        if memory is None:
            return None
        logger.info(f'归档对话 {conversation.id} → 记忆 ({action}): {content[:60]}')
        return memory
    except Exception as e:
        logger.warning(f'归档对话摘要写入记忆失败: {e}')
        return None


def extract_review_insights(conversation):
    """周回顾对话归档时提取回顾内容为记忆（亮点/改进方向）

    识别条件：对话标题以「周回顾」开头。
    提取策略：从用户消息中提取反思内容，分为亮点和改进方向两类。
    失败不阻断归档流程。
    """
    from .models import Memory

    try:
        # 只处理周回顾对话
        if not conversation.title.startswith('周回顾'):
            return []

        user_messages = list(
            conversation.messages.filter(role='user')
            .order_by('created_at')
            .values_list('content', flat=True)
        )
        if len(user_messages) < 1:
            return []

        # 检查是否已经提取过（幂等）
        if Memory.objects.filter(
            user=conversation.user,
            content__startswith='[周回顾]',
            source_message__conversation_id=conversation.id,
        ).exists():
            return []

        created = []
        # 把所有用户消息合并后提取
        full_text = '\n'.join(user_messages)[:800]

        # 简单启发式：包含「收获/完成/做到/开心/满意」的归为亮点
        highlight_keywords = ['收获', '完成', '做到', '开心', '满意', '进步', '成功', '坚持']
        # 包含「改进/下次/应该/可以更好/没做好」的归为改进方向
        improvement_keywords = ['改进', '下次', '应该', '可以更好', '没做好', '不足', '问题', '困难']

        highlight_parts = []
        improvement_parts = []

        for msg in user_messages:
            msg_lower = msg.strip()
            if any(kw in msg_lower for kw in highlight_keywords):
                highlight_parts.append(msg_lower[:150])
            elif any(kw in msg_lower for kw in improvement_keywords):
                improvement_parts.append(msg_lower[:150])

        # 如果分类不明确，把整体作为回顾记忆。
        # 三条都走门控（confidence 0.8 → active）：review 是用户自己写的反思，
        # 确定性高，但 denylist/敏感闸门照常生效
        for item in (
            {'content': f'[周回顾] {full_text[:400]}', 'category': 'other'}
            if not highlight_parts and not improvement_parts and full_text.strip()
            else None,
            {'content': '[本周亮点] ' + '；'.join(highlight_parts)[:400],
             'category': 'fact'} if highlight_parts else None,
            {'content': '[改进方向] ' + '；'.join(improvement_parts)[:400],
             'category': 'goal'} if improvement_parts else None,
        ):
            if not item:
                continue
            action, memory, _note = gate_and_write(
                conversation.user, {**item, 'importance': 5, 'confidence': 0.8})
            if memory is not None:
                created.append(memory)

        if created:
            logger.info(f'周回顾对话 {conversation.id} → 提取 {len(created)} 条记忆')
        return created

    except Exception as e:
        logger.warning(f'周回顾记忆提取失败: {e}')
        return []
