"""ROA PRD §10.2 评测集轻量落地（离线确定性版）

不复用 Django tests 形态：memory/tests.py 是功能锁（单点行为），
run_eval 是质量评测（30 条真实感夹具 + 换说法提问 + 指标门槛）。

评测的是**管线**而不是 AI 端到端（端到端要真调云端 Agent，慢且不确定，
不适合做回归卡点）：
- T1 召回：换说法提问的关键词走 retrieve_memories 检索路径，正确记忆
  必须进 top-5；同时全部夹具要出现在常驻注入里（覆盖 G5 预算裁剪）
- T2 取代失忆：supersede 之后旧值必须立即退出注入与检索（S4 门槛）
- T3 情景连续性：最近 episodic 进「最近聊过」块，更旧的不进
- T4 预算守恒：500 条记忆时注入仍 ≤ INJECTION_CHAR_BUDGET（G5）
- T5 纯度：注入里绝不出现 superseded/candidate/sensitive/他人记忆
- T6 敏感写入 0：无「记住」显式确认时，敏感样本必须 0 落库（§10.2 硬门槛）

每次改 prompt/阈值/权重后必须跑 `manage.py run_eval` 做前后对比，
任一指标回退不合入（写进 AGENTS.md 的长期纪律）。

用法：manage.py run_eval [--suite t1|t2|t3|t4|t5|t6|all] [--keep]
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from memory.services import (INJECTION_CHAR_BUDGET, build_memory_index_context,
                             gate_and_write, supersede_memory)
from memory.models import Memory

FACTS_PATH = Path(__file__).resolve().parent.parent.parent / 'eval' / 'facts.jsonl'

# PRD §10.2 MVP 门槛
GATE_RECALL = 0.85
GATE_HALLUCINATION = 0  # 纯度违规必须为 0
GATE_SENSITIVE_WRITES = 0  # 未确认的敏感写入必须为 0

SUITES = ('t1', 't2', 't3', 't4', 't5', 't6')

# T6 敏感样本：denylist（证件/卡号）+ assess_sensitivity（健康/财务），
# 全部**不带**「记住」字样发起 —— 期望 0 落库
T6_SAMPLES = [
    {'content': '我的身份证号是110101199003077890', 'category': 'other'},
    {'content': '我的银行卡号是6222020200112233445', 'category': 'other'},
    {'content': '我在服用舍曲林，每天半片', 'category': 'other'},
    {'content': '我的月薪是35000，年终奖两个月', 'category': 'other'},
    {'content': '我和前女友去年十月分手了', 'category': 'relationship'},
    {'content': '我房贷每个月还一万二', 'category': 'other'},
    {'content': '我的内网服务器地址是192.168.1.88', 'category': 'other'},
    {'content': '我的服务器登录密码是Ab12cd34', 'category': 'other'},
]


def load_facts():
    facts = []
    with open(FACTS_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                facts.append(json.loads(line))
    return facts


class Command(BaseCommand):
    help = 'ROA 记忆质量评测（T1-T6 离线版）：门槛召回≥85%、纯度违规 0、敏感写入 0'

    def add_arguments(self, parser):
        parser.add_argument('--suite', default='all',
                            choices=list(SUITES) + ['all'])
        parser.add_argument('--keep', action='store_true',
                            help='保留评测用户数据（默认跑完即删）')

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # 临时用户：隔离真实数据，跑完即删（CASCADE 清掉全部评测记忆）
        username = '__eval_tmp__'
        User.objects.filter(username=username).delete()
        user = User.objects.create_user(username=username, password='!')
        self.stdout.write(f'评测用户 #{user.id}（{len(load_facts())} 条夹具）\n')

        suites = SUITES if opts['suite'] == 'all' else (opts['suite'],)
        results = {}
        try:
            if 't1' in suites or 't5' in suites:
                # T1/T5 共用夹具库：全部走 gate_and_write（confidence 0.9 → active）
                self._facts = self._write_facts(user)
            if 't1' in suites:
                results['t1'] = self._run_t1(user)
            if 't2' in suites:
                results['t2'] = self._run_t2(user)
            if 't3' in suites:
                results['t3'] = self._run_t3(user)
            if 't4' in suites:
                results['t4'] = self._run_t4(user)
            if 't5' in suites:
                results['t5'] = self._run_t5(user)
            if 't6' in suites:
                results['t6'] = self._run_t6(user)
        finally:
            if not opts['keep']:
                user.delete()

        self._report(results, suites)
        if any(not r['ok'] for r in results.values()):
            raise SystemExit(1)

    # ── T1：换说法召回 ──────────────────────────────
    def _write_facts(self, user):
        facts, memories = load_facts(), {}
        for i, fact in enumerate(facts):
            action, memory, _note = gate_and_write(user, {
                'content': fact['content'], 'type': fact['type'],
                'category': fact['category'], 'importance': fact['importance'],
                'confidence': 0.9,
            })
            if action != 'written':
                raise SystemExit(f'夹具 #{i} 写入异常（{action}）: {fact["content"]}')
            memories[fact['content']] = memory
        return memories

    def _run_t1(self, user):
        from memory.services import retrieve_memories

        facts = load_facts()
        probe_total = hits = 0
        misses = []
        for fact in facts:
            memory = self._facts[fact['content']]
            for probe in fact['probes']:
                probe_total += 1
                # 模拟 AI 选关键词调 memory_search 的检索路径：
                # 任一关键词命中且正确记忆进 top-5 即算召回
                found = False
                for key in probe['keys']:
                    ids = [m.id for m in retrieve_memories(user, query=key, limit=5)]
                    if memory.id in ids:
                        found = True
                        break
                if found:
                    hits += 1
                else:
                    misses.append(f'{probe["q"]} ←「{fact["content"]}」')

        # 注入覆盖：全部夹具要出现在常驻注入里（预算裁剪不掉高重要度事实）
        ctx = build_memory_index_context(user)
        uncovered = [c for c in self._facts if c not in ctx]
        recall = hits / probe_total if probe_total else 1.0
        ok = recall >= GATE_RECALL and not uncovered
        return {
            'ok': ok,
            'detail': (f'检索召回 {hits}/{probe_total} = {recall:.0%}'
                       f'（门槛 {GATE_RECALL:.0%}）· 注入覆盖 '
                       f'{len(self._facts) - len(uncovered)}/{len(self._facts)}'),
            'misses': misses[:8] + uncovered[:4],
        }

    # ── T2：取代失忆（S4）──────────────────────────
    def _run_t2(self, user):
        facts = load_facts()
        old_content = facts[0]['content']
        old = self._facts[old_content]
        new_content = '用户对花生和腰果都过敏，坚果类一律不碰'
        action, new, _note = supersede_memory(
            user, old, {'content': new_content, 'confidence': 0.9})

        ctx = build_memory_index_context(user)
        from memory.services import retrieve_memories
        retrieved = [m.content for m in retrieve_memories(user, query='花生', limit=10)]

        problems = []
        if action not in ('written', 'candidate'):
            problems.append(f'取代未生效：{action}')
        if old_content in ctx:
            problems.append('旧值仍出现在常驻注入')
        if old_content in retrieved:
            problems.append('旧值仍出现在检索结果')
        if new_content not in ctx:
            problems.append('新值没有进入注入')
        return {'ok': not problems, 'detail': '取代链生效' if not problems else '；'.join(problems)}

    # ── T3：情景连续性 ─────────────────────────────
    def _run_t3(self, user):
        titles = ['讨论了搬家预算', '讨论了年假安排', '讨论了体检复查', '讨论了很早的旧事']
        mems = []
        for i, t in enumerate(titles):
            action, m, _note = gate_and_write(
                user, {'content': f'上次对话{t}，结论已明确', 'type': 'episodic',
                       'confidence': 0.9})
            mems.append(m)
        # 把第 4 条顶到最旧：必须走 queryset.update——updated_at 是
        # auto_now 字段，save(update_fields=...) 会把它重置回当前时间
        Memory.objects.filter(id=mems[3].id).update(
            updated_at=timezone.now() - timezone.timedelta(days=30))

        ctx = build_memory_index_context(user)
        problems = []
        for m in mems[:3]:
            if m.content not in ctx:
                problems.append(f'近期情景未注入：{m.content}')
        if mems[3].content in ctx:
            problems.append('更旧的情景不应注入')
        return {'ok': not problems, 'detail': '近 3 条情景注入' if not problems else '；'.join(problems)}

    # ── T4：预算守恒（G5）──────────────────────────
    def _run_t4(self, user):
        bulk = [Memory(user=user, content=f'填充记忆第 {i} 号：一些不太重要的背景信息',
                       category='other', importance=3, confidence=0.9)
                for i in range(500)]
        Memory.objects.bulk_create(bulk)

        ctx = build_memory_index_context(user)
        overflow = len(ctx) - INJECTION_CHAR_BUDGET
        ok = len(ctx) <= INJECTION_CHAR_BUDGET
        return {'ok': ok,
                'detail': (f'503 条记忆时注入 {len(ctx)}/{INJECTION_CHAR_BUDGET} 字符'
                           + (f'（超 {overflow}）' if overflow > 0 else ''))}

    # ── T5：注入纯度（幻觉面代理）──────────────────
    def _run_t5(self, user):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        other = User.objects.create_user(username='__eval_other__', password='!')
        try:
            Memory.objects.create(user=other, content='他人隐私：别人的秘密事项甲乙丙',
                                  category='other')
            # 构造一条 superseded + 一条 candidate + 一条 sensitive
            sup = Memory.objects.create(user=user, content='已被更正的旧地址：朝阳区某路',
                                        category='other', confidence=0.9)
            supersede_memory(user, sup, {'content': '用户现住海淀区某小区', 'confidence': 0.9})
            Memory.objects.create(user=user, content='待复验的存疑事项：可能去成都出差',
                                  category='other', status='candidate')
            Memory.objects.create(user=user, content='敏感未确认事项：心里有些焦虑情绪',
                                  category='other', sensitivity='sensitive')

            ctx = build_memory_index_context(user)
            leaks = []
            for marker in ('朝阳区某路', '可能去成都出差', '心里有些焦虑情绪',
                           '别人的秘密事项甲乙丙'):
                if marker in ctx:
                    leaks.append(marker)
            return {'ok': not leaks,
                    'detail': '注入无越权/过时/未确认内容' if not leaks
                    else f'纯度违规 {len(leaks)} 处（门槛 0）：{leaks}'}
        finally:
            other.delete()

    # ── T6：敏感写入 0（§10.2 硬门槛）──────────────
    def _run_t6(self, user):
        unauthorized = []
        for sample in T6_SAMPLES:
            action, _m, _note = gate_and_write(user, dict(sample))
            if action in ('written', 'candidate', 'merged'):
                unauthorized.append(sample['content'])

        # 对照组：显式「记住」后应放行（闸门不是一刀切拒绝）
        action, _m, _note = gate_and_write(
            user, {'content': '我在服用维生素D补充剂，每天一粒', 'sensitivity': 'sensitive'},
            user_text='记住：我在服用维生素D补充剂，每天一粒')
        consent_ok = action in ('written', 'candidate')

        ok = len(unauthorized) == GATE_SENSITIVE_WRITES and consent_ok
        detail = (f'敏感样本 {len(T6_SAMPLES)} 条未确认写入 '
                  f'{len(unauthorized)}（门槛 0）· 显式确认放行：{"是" if consent_ok else "否"}')
        return {'ok': ok, 'detail': detail, 'misses': unauthorized}

    # ── 报告 ───────────────────────────────────────
    def _report(self, results, suites):
        titles = {'t1': 'T1 换说法召回', 't2': 'T2 取代失忆', 't3': 'T3 情景连续性',
                  't4': 'T4 预算守恒', 't5': 'T5 注入纯度', 't6': 'T6 敏感写入'}
        self.stdout.write('')
        for suite in suites:
            r = results.get(suite)
            if not r:
                continue
            mark = '✓' if r['ok'] else '✗'
            self.stdout.write(f'{mark} {titles[suite]}：{r["detail"]}')
            for miss in r.get('misses', []):
                self.stdout.write(f'    · {miss}')
        self.stdout.write('')
        failed = [s for s in suites if results.get(s) and not results[s]['ok']]
        self.stdout.write('结论：全部达标' if not failed else f'结论：未达标 {failed}（exit 1）')
