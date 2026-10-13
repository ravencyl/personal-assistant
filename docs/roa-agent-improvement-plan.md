# 三磊 × ROA PRD 对比与改进方案

- PRD 来源：飞书 wiki《PRD · 个人长期 Agent（代号 ROA）》v1.0（2026-10-12，`my.feishu.cn/wiki/FnX9weIxpijdLxkcWDDc24ONn8g`）
- 对比基线：当前「个人助手」站点（ravenclaw.top）的 chat / memory / agents / knowledge 模块，证据以代码为准（文中括注文件与行号）
- 日期：2026-10-13

## 0. 一句话结论

ROA PRD 描述的系统，**载体层**（自建个人服务器、Web + 移动端、单人使用、Agent 对话、工具协议、记忆读写闭环）当前站点已经全部具备，且对话工程细节（异步 turn 状态机、确认卡、协议截断救援、$LAST_REPLY 引用）比 PRD 假设的实现更成熟。**真正的差距集中在「记忆质量治理」一条线**：写入门控、procedural 独立分层、取代链、遗忘归档、每轮记忆注入、元规则、评测集——这恰好是 PRD 自己声称的重点（§0「§3/§5/§10 决定三个月后是越来越懂我还是越来越乱」）。

所以本方案的主张是：**不新建 ROA，就地演进三磊的 memory 模块**，把 PRD 的治理决策移植进来；PRD 中与现状冲突或性价比低的决策（文件为真相源、独立部署、3-2-1 备份）给出保留意见。

## 1. 总体对照表

| 维度 | PRD 决策 | 当前现状（代码证据） | 差距判定 | 优先级 |
|------|----------|----------------------|----------|--------|
| 载体/部署 | 个人服务器 + Web + 移动优先 + HTTPS | ravenclaw.top：nginx + gunicorn + PWA + 移动端 Tab（deploy skill / AGENTS.md） | ✅ 已对齐 | — |
| 用户模型 | 单人自用，不做多租户 | 有 visible_qs 超管/普通用户框架，实际单人 | ⚠️ 现状多一层但无害，保留 | — |
| Agent 主循环 | Observe→Retrieve→Plan→Act→Reflect→Write→Report | 云端 Qoder Agent + 编排器意图分发（core/agent_registry.py），单轮 dispatch | ⚠️ 形态不同但等价能力在线（工具、记忆字段、补发通道都有），不重造 | — |
| 工具契约 | memory_search/fetch/write/supersede/forget，写必经门控 | memory.search / memory.update（含 void）+ 协议 `memory` 字段直落库（agent_registry.py:508-518） | 🔴 写入无门控；无 supersede/forget 语义（void=硬删） | P0 |
| 记忆分层 | working/episodic/semantic/procedural 四层，procedural 独立成层 | 一张平表 + 6 个 category（memory/models.py:7-14），纠正类规则没有专门归属 | 🔴 结构性缺口，PRD 称之为「最重要的结构决策」 | P0 |
| 作用域 scope | global / project:<id> / domain:<tag> | 无 scope 字段；但对话有 pin_activity 钉选（会话级项目锚点） | 🟠 可借 pin/activity 体系落地，非零成本新建 | P1 |
| 条目 Schema | hook/confidence/salience/sensitivity/status/valid_from/invalid_at/superseded_by/recall_count/review_flag/ttl | content/category/importance(1-10)/source_message/access_count/last_accessed/consolidated | 🟠 缺 9 个字段；importance≈salience、access_count≈recall_count 可映射，其余要迁移新增 | P0 |
| 索引常驻注入 | INDEX.md（title+hook，≤200 行）+ high 全文，预算 ≤2.5k token | 建对话时注入 top 10 条**全文**（chat/views.py:268-272），每轮 turn_prompt **不再注入**（_build_ai_content 无记忆通道，665-707 行） | 🔴 双重问题：存量会话记忆永不更新（与首帧协议同款陷阱，AGENTS.md 已实录过）；无 hook/索引，预算按条数粗控 | P0 |
| 热路径写入 | 规则预筛→LLM 抽取→门控（confidence 分档/sensitive 确认/denylist）→近似合并 | 规则预筛有（EXTRACTION_PATTERNS 正则 12 条）+ AI 主动提取有（协议 memory 字段），**门控全缺**：命中即 active 落库；近似去重有（char_overlap_ratio>0.8）但只「跳过」不「合并计数」 | 🟠 骨架对，缺闸门 | P0 |
| 矛盾消解 | 取代链：invalid_at + superseded_by + status=superseded，四种关系判定 | memory_update 直接改写内容或硬删（agent_tools.py:75-81）；周聚合 prompt 里一句「时间新的为准」（consolidate_memories.py:31-33），被合的旧条只标 consolidated=True | 🔴 无取代语义、无历史留痕，改错了不可回看 | P0 |
| 遗忘 | 保留分公式 + 每周批扫 archive + TTL | 衰减只参与排序（_memory_score，services.py:53-64），永不归档/删除；无 TTL | 🟠 公式可平移，缺批扫命令 + status 字段 | P1 |
| 冷路径整合 | 每日蒸馏 + 变更报告 + 可回滚 | 每周日 cron 按 (user,category) ≥3 条 AI 合成画像，标 consolidated（软回滚成立），**无变更报告** | 🟠 实现了一半 | P1 |
| Episode | 结构化（summary/outcome/decisions/open_items）+ 启动注入近 3 条 | 归档对话写一条启发式摘要（首问+末答，importance=4，services.py:265-321）——原型即雏形；无结构化字段、无「近 N 条」注入 | 🟠 升级现有摘要即可，不必新存储 | P1 |
| 主动复验 §5.6 | 引用外部可验证对象前先廉价验证 | 无系统侧机制（AGENTS.md 的「推荐前先 grep」是给 Qoder 的我看的，不是给站点 Agent 的） | 🟡 可在协议 prompt 加一条行为约束，成本低 | P1 |
| 记忆元规则 §8.3 | 时点非当下/冲突以观察为准/记忆≠授权/查不到就说不知道 | 协议有「不要凭印象编造」「自然运用不要主动提及」（agent_registry.py:329/363、services.py:127）；四条硬元规则均缺 | 🔴 半缺，且属于「写进 prompt 就有收益」的最低成本项 | P0 |
| 隐私闸门 | sensitivity=sensitive 需显式确认；数据红线（内网标识正则拦截） | 无任何过滤，聊天内容可原样落记忆并随上下文外发到云端 API | 🔴 单人站点同样成立：这是公网服务器 + 第三方模型 | P0 |
| 可审计/导出 | append-only 审计 + 一键导出 + 全量浏览 UI | 只有 logger.info；浏览/编辑/删除页有（templates/memory/ + memory_update 候选卡）；无导出 | 🟠 审计与导出缺 | P1 |
| 溯源 UI | 正文角标 citation spans → 溯源浮层 | source_message FK 字段存在但界面不展示；citation spans 无 | 🟡 最难的放最后：先做「条目详情显示来源消息链接」的降级版 | P2 |
| 检索升级 | pgvector+BM25 混合，仅 >800 条或评测不达标才启用 | icontains 关键词（工具与注入同口径） | ✅ 与 PRD「向量后置」立场一致，现状可接受 | — |
| 评测 | T1-T6 六个评测集 + 指标门槛 + 回归卡点 | 零。现有 946 行 memory/tests.py 是功能锁不是质量评测 | 🔴 PRD 方法论核心，完全缺失 | P1 |

## 2. 已经对齐、不要动的部分

1. **载体与部署形态**（PRD §4.4/§9.3）：个人服务器、强制 HTTPS、移动端优先、定时任务服务端跑——全部已是现状。PRD Phase 0 的「Docker Compose 起后端+Postgres」对现状是横向替换（SQLite→PG），没有功能收益，不做。
2. **对话工程**：异步 turn、确认卡两步流、协议截断救援、长轮询 turn-wait——PRD 没写到这个深度，照搬任何「重写主循环」的想法都是倒退。
3. **检索策略立场**：PRD 明确把向量检索排第三并推迟启用（§4.3），现状「索引注入 + 关键词主动检索」正好是 PRD 推荐的起步形态。不要有人提议加 pgvector 就去做。
4. **记忆 UI 基础**：浏览/编辑/删除界面、候选卡选条、`_is_similar_content` 去重、访问计数强化——PRD G4「可见可删」已满足大半。

## 3. 核心差距的工程落点（改进方案主体）

### 3.1 P0-A：Schema 分层与扩展（一次迁移解决）

`memory/models.py` 增加字段，全部给默认值，向后兼容：

- `type`：`semantic`（默认，现有 preference/fact/… 全部归此）| `procedural` | `episodic`。category 保留做细分。**procedural 是新增的第 7 类语义层，不是新模型**。
- `hook`：≤150 字一句话（索引行用；写入时由提取方必填，缺了用 content 截断兜底）。
- `confidence`（0-1，默认按 importance/10 折算）、`salience`（high/normal/low，默认 normal）、`sensitivity`（默认 normal）、`status`（active/candidate/stale/superseded/archived/expired，默认 active）、`valid_from`/`invalid_at`/`superseded_by`（默认空）、`review_flag`（默认 False）、`last_verified`（默认 created_at）。
- 现 `access_count`/`importance` 不更名——PRD 的 recall_count/salience 语义映射过去即可，避免连锁改注入排序。

写入侧同步改两处：协议 `memory` 字段与 `EXTRACTION_PATTERNS` 命中「别再/以后不要/下次…点」句式时落 `type=procedural`（这是 S2 场景「一次纠正终身生效」的前提）；`save_ai_extracted_memories` 接受 hook/type/confidence/sensitivity。

### 3.2 P0-B：写入门控（Gate）

现状「命中即落库」是唯一写入路径，按 PRD §5.2 在 `memory/services.py` 收口成一份 `gate_and_write(user, candidate)`：

1. **denylist 前置**（§5.2.1 + §9.3.1 数据红线）：证件号/密码/卡号正则、内网域名模式（`.loreal.`、`.corp.`、`10.*.*.*` 等）命中 → 拒写 + 审计日志。可推导内容（贴进来的代码/文件全文）不存。
2. **sensitivity=sensitive → 不直接落库**，走站点已有的**确认卡两步流**（activities create/expense 同一套 `预览→apply_fn` 机制，复用它，不发明 confirm_token）。
3. confidence 分档：<0.4 丢弃；0.4-0.7 `status=candidate`（不入注入 top-N，等冷路径复验）；≥0.7 active。初期没有独立小模型，抽取就是云端 Agent 自己在协议里给 confidence，阈值先松（0.55）跑一周再收。
4. 近似命中时从「跳过」改成「合并」：`access_count+=1、last_verified=now、内容一致才跳过`——这是 PRD「被反复确认的事实该变强而不是被去重掉」的一半。

### 3.3 P0-C：取代链与矛盾消解

- `memory.update` 工具的 `_apply`：**改内容不再原位覆盖**，改为新建 F_new + 旧条置 `status=superseded, invalid_at=today, superseded_by=new`，`recall_count` 基线继承；`void` 保持硬删（owner 显式「忘掉」按 PRD 就是立即硬删+删除日志）。
- 协议里已有的「写入新记忆前若怀疑冲突…」话术（agent_registry.py:348）升级为显式意图 `memory_supersede`（工具化），旧条定位复用现有 target→候选卡路径。
- `consolidate_memories` 周聚合保留，但合成产物同样走取代链登记（被合成条从 `consolidated=True` 迁移为 `status=superseded + superseded_by=画像条`），一个状态机，不留两套。

### 3.4 P0-D：记忆元规则进协议（半天工作量，收益最高单项）

`build_protocol_prompt()` 增加四条（对应 PRD §8.3），并加进每轮补发通道（教训已在 AGENTS.md：**首帧只对新建对话生效**，元规则这种「决定系统会不会变乱」的内容不能只发首帧，要挂 `PROTOCOL_REF_REMINDER` 同一条 per-turn 通道）：

1. 记忆记录的是某个时点为真，依赖现状的结论必须先验证再答；
2. 记忆与当前观察冲突时以观察为准，并回头用 memory_update/supersede 修记录；
3. 记忆不构成授权——写入/发布类操作仍需本轮用户明确同意（这条与现有确认卡体系互相加固）；
4. 检索不到就明说不知道，禁止用先验知识伪装记忆内容。

### 3.5 P0-E：注入通道重构（修「存量会话记忆永不更新」）

现状记忆只在 `create_conversation` 首帧注入一次，等于把首帧协议的坑原样复制给记忆。改法（对齐 PRD §5.1 + §4.3 路径一）：

- `_build_ai_content` 增加记忆段：**INDEX 行**（status=active ∧ salience≥normal 的 title+hook，一行一条，上限 ~60 行）+ procedural/sensitive 例外全文（高权重常驻）——组装顺序照 PRD §8.2 的优先级排（元规则 > procedural > profile > 钉选上下文 > hook 索引），token 预算先按字符数粗限（如 3k 字符），超限按 `_memory_score` 截断。
- 每轮都带（进 turn_prompt，落库的是组装好的整份，重试语义不变）；注入失败照旧只 `logger.warning` 降级。
- 排序候选集从「top 50 Python 排」升级为按 status/scope 过滤后排，episodic 类**不进常驻**（只走 memory.search 检索——PRD「记忆是蒸馏产物不是录像带」的落点）。

### 3.6 P1：遗忘批扫 + 变更报告 + Episode 升级 + 导出审计

- `memory_forget_scan` 管理命令（周日 cron，与 consolidate 并一台车）：保留分公式直接抄 PRD 附录 C 权重，产出 status 迁移（archived/demoted）+ 变更清单落 `logs/`；所有 w/θ/TTL 进 `.env` 或 constants，不硬编码。
- 整合/取代/遗忘统一记一张 `MemoryAudit` append-only 表（动作/依据条/时间/前后值摘要）——同时兑现 G4「可溯源」与 D.2 浮层的数据源；「一键导出」= 按 schema dump 全部 active 条目为 Markdown 文件（zip 下载），半小时工作量。
- 归档摘要升级为结构化 episode（`content` 前缀约定或 payload 化），新建对话时注入近 3 条 episode 摘要（补 §5.1 第 4 步跨会话连续性）。
- 主动复验 §5.6：v1 只做协议规则版（引用涉及路径/配置/现状前先查证），不做系统侧触发器。

### 3.7 P1：评测集 T1-T6（PRD 方法论的移植）

不复用 Django tests 形态，新建轻量夹具评测：`memory/eval/` 下 `facts.jsonl`（30 条写死事实 + 每条 2-3 个换说法提问）、`run_eval.py` 管理命令（批量建临时对话→发问→判命中）。MVP 门槛照抄 PRD §10.2（召回 ≥85%、幻觉 ≤5%、敏感写入必须 0——最后这条接 3.2 denylist 的用例）。每次改 prompt/阈值/权重必须跑前后对比，任一指标回退不合入——这条写进 AGENTS.md 成为长期纪律。

### 3.8 P2：溯源 UI 与记忆面板（附录 D 三张稿的取舍）

- **降级版 D.2 先做**：memory_list 条目详情展示 `source_message` 链接（「它凭什么这么记」一屏自证）；正文 citation spans 需要模型稳定输出引用偏移，PRD 自己都说「拿不准宁可不标」，列为观察项不做。
- D.1 三栏「右栏常驻记忆索引」：聊天分栏页桌面端右 rail 可加折叠的「本次已加载 N 条记忆」面板（数据即 3.5 注入清单，G5 预算的可视化）——PWA 场景做，不与活动右 rail 抢位。
- D.3 护栏双态：取代链抽屉 = MemoryAudit + superseded_by 递归查询的 UI 化，做在 memory_list 页即可，不必进消息流（PRD 的「系统事件条」形态适合独立产品，站点里审计页更省）。

## 4. 不建议照搬 PRD 的四个决策（保留意见）

1. **文件为真相源（§4.2）**：PRD 的理由是 owner 可读可改可 git。现状 SQLite 单表 + 完整浏览/编辑 UI + Admin，「可读可改」已满足；3.6 的 Markdown 导出补足「可 git/可迁移」。为文件化重写存储层与并发锁，收益低于成本，**不改**，导出目录进 git 即得 diff 能力。
2. **新服务新库（Postgres+pgvector、Docker Compose 重建）**：单人负载下换栈是纯运维风险，且生产刚经历过 venv 重建事故（见记忆 venv-console-script-shebang）。**就地演进**。
3. **3-2-1 每日备份（§4.4）**：与用户已定决策「生产不做定期数据库备份」直接冲突（记忆 cron.md）。本方案用 git 化导出目录（仅记忆文本，不含全站业务数据）部分覆盖「记忆不可逆损毁」风险 R5，**不恢复定期全量备份**；如用户读到 §12 R5 后改主意，需用户显式推翻旧决定。
4. **不协助规避企业网关（§9.3）**：同意且现状本来如此，无需动作；同时提醒——站点数据（含记忆）经云端 API 出境推理已是既定架构，PRD §9.1「记忆不外发」与现状（Qoder Cloud）矛盾，现实解就是 §9.3.1 数据红线：**防的是不该进记忆的内容，靠 3.2 denylist 而不是防外发**。

## 5. 路线图与判定（映射 PRD Phase 0/1/2，压缩为三批）

| 批次 | 内容 | 判定门槛（PRD 口径） |
|------|------|----------------------|
| A（1 周内可上线） | 3.1 Schema 迁移 + 3.2 门控/denylist/敏感确认卡 + 3.4 元规则（含每轮补发）+ 3.5 每轮注入重构 | S2：一次纠正落 procedural 后新会话同类任务不再犯；T6 抽 8 条敏感对话跑通「0 写入」；存量老对话刷新后能体现新记忆（注入通道验证） |
| B（+1~2 周） | 3.3 取代链 + 3.6 遗忘批扫/审计表/导出/episode 注入 | S4：项目状态改写 24h 内旧值不再被当现状引用（superseded 不进注入）；记忆 500 条时注入仍 ≤ 预算；每次整合有报告可回滚 |
| C（按需） | 3.7 评测集 T1-T6 + 3.8 溯源 UI/记忆索引侧栏 | T1 换说法命中 ≥85%；T5 幻觉记忆 ≤5%；「它记了我什么」从点击到答案 ≤2 跳 |

依赖：A→B→C 串行（与 PRD P1→P2→P3 同理，门控没建就谈遗忘/评测没有数据基础）。每批走站点既有纪律：迁移先临时快照、测试锁 + 变异反证、`manage.py test memory chat` 全绿后自动四步上线。

## 6. 每批的回归锁建议（对齐项目测试文化）

- A：`MemoryGateTest`（denylist 命中不入库、sensitive 走确认不落库、confidence 分档落 status）、`MemorySchemaMigrationTest`（historical models 重放，参考既有四字段删除的锁模式）、`ProtocolMetaRulesTest`（四条元规则在首帧**和**每轮补发通道都在——复用 `StaleSessionReferenceReminderTest` 的形状）、`InjectionPerTurnTest`（存量对话发下一条消息时 turn_prompt 含最新 active 记忆）。
- B：`SupersedeChainTest`（update 内容产生取代链、旧条 status/invalid_at/superseded_by 齐全、注入不取 superseded）、`ForgetScanTest`（阈值外归档、owner「忘掉」仍硬删）、`MemoryAuditAppendOnlyTest`、`MemoryExportTest`（导出文件含全部 active 且可 reload）。
- C：`MemoryEvalHarnessTest`（跑夹具不跑真模型，判分器与检索路径离线可测）。

## 7. 开放问题对照（PRD §13 → 本站点立场）

| PRD 开放问题 | 本站点当前立场 |
|--------------|----------------|
| 是否收敛到覆盖网（Tailscale） | 保留公网 + Django session 鉴权；可加项：登录失败退避与 /chat 接口限流（nginx `limit_req`），属 §4.4 最小防护未完成部分 |
| 双供应商降级 | 不做。协议层已对云端故障高容错（降级文本/救援），换供应商是独立工程 |
| 原始转录保留期 | 现状消息永久保留，比 PRD 的 90 天激进；单人自用可接受，若做隐私收口先收 episodic 注入而不收存储 |
| agent 主动提问澄清记忆 | 采纳「存疑条目下次会话主动求证」，实现即 3.5 注入时带 review_flag 条 + 元规则允许提问；限每日一次的约束可后补 |
| 记忆存指针还是摘要 | 与现状一致：正文类资料本来就在 knowledge/activities，记忆只写「关于我的事实」；写进 denylist 话术（可推导的不存） |
| 人格层 | 不做独立管理，交给 AI 自我介绍 prompt（settings.SITE_NAME 一处口径） |

---

### 附：证据索引（现状关键实现位置）

- 记忆模型与全部字段：`memory/models.py:5-53`
- 规则预筛正则 12 条：`memory/services.py:21-35`；衰减排序：`53-64`；注入格式化：`112-132`；近似去重（只跳过）：`139-155`；归档摘要：`265-321`
- AI 写入路径（无门控直落 active）：`core/agent_registry.py:343`（协议字段）、`508-518`（落库）
- 记忆工具（search=icontains；update=原位改写/硬删）：`memory/agent_tools.py:16-156`
- 首帧一次性注入：`chat/views.py:267-273`；每轮组装不含记忆：`chat/views.py:665-707`
- 周聚合（prompt 级时间裁决 + consolidated 标记）：`memory/management/commands/consolidate_memories.py:21-33,95-136`
