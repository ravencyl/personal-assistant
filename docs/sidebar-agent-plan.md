# 侧边栏 AI 助手（整站 Agent 辅助）设计方案

> 2026-11-08 · 仅设计，未实施。定稿后再拆任务。
> 已拍板：D2 升级为**整体 UI 调整（双栏 shell）**、D3 **常驻单会话**（2026-11-08 用户确认）。

## 0. 一句话定位

把现有 AI 对话能力装进一个**整站常驻的 PC 左侧边栏**：用户在任何页面展开它，用自然语言对活动等业务数据下指令；AI 执行后，**右侧页面确定性联动**（跳详情 / 刷新 / 高亮），形成「左边聊、右边看」的副驾驶体验。

## 1. 现状对账：哪些已经有了，哪些是新的

| 能力 | 现状 | 结论 |
|------|------|------|
| 活动的建/改/删/记费用 AI 工具 | `activities/agent_tools.py`，两步确认流（预览→apply） | **复用，零新概念** |
| 异步 turn 收发引擎 | `chat-turn.js::PaChatTurn`（分栏页/详情页/问 AI 抽屉三挂载点共用） | 复用，侧边栏是第四挂载点 |
| 页面上下文感知雏形 | `base.html::paPageContext`（正则识别 4 种页面）→ send 的 `page_context` 字段拼进 ai_content | 有，但要升级（见 D4） |
| 写后右侧反应雏形 | `activities:changed` 事件 → 提示条 + 活动系页面 3s 自动 reload；HTMX 响应里 `data-activity-changed` 标记同链 | 有，是 page_actions 的地基 |
| 侧边栏宿主 UI | 无（问 AI 抽屉是详情页右侧、单活动场景） | **新** |
| 确定性页面反应协议 | 无（目前只有「整页 reload」一种粗粒度反应） | **新**（见 D5，本方案核心） |

## 2. 核心决策

### D1 · 不拆多 Agent

沿用单 Qoder Cloud Agent + `INTENT_TOOL_MAP` 意图分发（2026-11-08 已论证：工具层天然按 app 隔离、协议层模块化；拆分引入路由失败点与两倍协议漂移面）。侧边栏只是**新宿主**，不是新大脑。

### D2 · 侧边栏形态：全高左栏 shell（已定稿：接受整体 UI 调整）

侧边栏不是浮在页面上的面板，而是**整站 PC 布局的组成部分**——升级为「左侧 AI 栏 + 右侧站点」的双栏 shell：

- **收起态**：侧栏宽度 0，左缘中部常驻一个 AI 把手（约 44px 触控区），点击展开；不加全局快捷键（用户已禁全局 keydown）。
- **展开态**：360px 全高左栏（fixed，含自有头部），右侧站点整体右移（wrapper `margin-left: 360px` + 0.2s transition），顶栏在剩余区域内继续 sticky——视觉上是「AI 栏 + 完整站点」并排，内容**零遮挡**，边聊边看。
- z-index：侧栏 50 > 顶栏 40（全高侧栏需盖过顶栏滚动区）；全局搜索 overlay 60 仍在最上层。
- 仅 PC：`hidden md:block` 成对块，移动端不渲染（移动端对话入口不变）。

### D2.1 · 整体 UI 调整清单（shell 化的牵连点）

| 牵连点 | 调整 |
|--------|------|
| 顶栏 | 跟随 wrapper 右移；展开时横跨剩余区域（logo/导航/搜索照常可用） |
| `max-w-7xl` 内容容器 | `mx-auto` 自适应剩余宽度，零改动 |
| 桌面两列页（`.page-cols`） | 宽度预算：1440 屏展开后左列约 664px（舒适）；1280 屏约 504px（偏紧但可读）。浏览器实测定稿；必要时展开态临时收窄右列作备选，不进 P1 |
| 「活动数据已更新」提示条 | 展开时居中基准偏移侧栏宽的一半（`left: calc(50% + 180px)`），P1 顺手修 |
| 全局搜索 overlay | `fixed inset-0` 横跨全屏（含侧栏区），可接受不改 |
| 移动端 | 完全不动 |

### D2.2 · 侧栏自身 UI 规格

- **头部**：「AI 助手」标识 + 「新话题」按钮 + 收起（×）。
- **消息流**：复用 `_message.html`（卡片 / chips / 确认卡同主对话页一份渲染）。
- **输入区**：textarea（Enter 换行、点发送、`paFitTextarea` 限高内滚动）；@ 钉选 P2 接入。
- **视觉**：Typeframe 令牌（白底黑字、`var(--border)` 分隔、卡片面档位），与右侧内容之间一条 `var(--border)`；新增类名要跑 Tailwind 构建。

### D3 · 会话策略：常驻单会话（已定稿）

- 侧边栏 create-or-get「最近一条非归档会话」：有就续聊，没有就新建（首问截断做标题的既有逻辑）。
- 头部放「新话题」按钮（开新会话），**不复制完整会话列表**（那是主对话页职责）；用户在主对话页能看到侧边栏聊的全部历史——跨入口续聊是 feature。
- 与「问 AI」抽屉并存不合并：抽屉 = 单活动专属会话（右侧滑出、每活动一个）；侧边栏 = 全局助手（左侧、跨页面）。两者共用 `PaChatTurn` / 消息模板 / 卡片模板。
- 跳页恢复（联动体验的关键）：展开状态与会话 id 记 localStorage；AI 触发 navigate 后整页刷新，新页面初始化脚本读到状态 → 自动展开 + 拉历史 + `turn_resume` 接轮询（既有模式，无新机制）。

### D4 · 页面上下文：前端只带 pathname，服务端反解

现有 `paPageContext` 正则方案每加页面就要改 base.html，且只覆盖 4 种页面。升级为：

- 前端 send 时附 `page_path = location.pathname`（一行 JS，全站通用）。
- 服务端 `django.urls.resolve(page_path)` 反解 → `url_name` + `kwargs` → 构造一行结构化上下文（如：`用户正在浏览：活动详情页《桐庐旅行》(ID 28)`；对象名用 `get_visible` 查一次 PK，普通用户只看得到自己的，天然合规）。resolve 失败 → 只给 pathname。
- 注入进 send 组装的 `ai_content`（turn_prompt 落库，重试重发确定性，与现 `page_context` 同通道）；过渡期兼容：先读 `page_path`，缺省回落旧 `page_context` 字段，全量后删正则方案。
- **不注入页面正文/HTML**：token 预算 + prompt 注入面双保险，只给路由与对象元数据。

### D5 · 页面反应协议 page_actions（核心新东西）

三种声明式动作，**服务端确定性生成，不进模型协议 JSON**（模型只说人话，动作由代码构造——协议截断/残骸教训的直接应用，协议面积零增加）：

| type | 载荷 | 右侧页面行为 |
|------|------|--------------|
| `navigate` | `{url}` | 整页跳转到目标（URL 必由 `reverse()` 生成 + 前端同源校验双保险） |
| `refresh` | `{}` | 复用 `activities:changed` 提示条 + 3s 自动 reload（既有机制零新代码） |
| `highlight` | `{target_id}` | 列表页滚动到 `data-activity-id` 行并闪烁一下；找不到静默跳过 |

**统一生成规则**（集中在 apply 成功分支的一个函数里）：

> AI 碰了对象 X → 右侧不在 X 的页面 → `navigate` 到 X；已在 X 的页面 → `refresh`。

具体到写工具：create → navigate 新活动详情；update / add_expense / 改状态 → 上述规则；delete → navigate 回列表。查询类工具一律不产生 page_actions（查询要安静，不过度跳转）。

**下发通道（零新增 JS 通道）**：写操作全走两步确认，所以 page_actions 的主消费点在 **apply 端点成功响应的 HTMX 片段**里——渲染一个 `data-page-action='{"type":"navigate","url":…}'` 隐藏标记；base.html 已有的 document 级 `htmx:afterSwap` 监听扩展为：读到标记 → 执行对应动作。主对话页 / 详情页 / 侧边栏三处的确认卡共用同一机制，侧边栏确认卡原样复用 `_confirm_actions.html` 的 hx-post。

**与既有 changed 广播的冲突处理**：page_actions 带 navigate 时抑制「活动数据已更新」提示条（跳页本身就拿到了新数据，弹条再 3 秒 reload 是双重反应）。

## 3. 端到端流程（用户例子走一遍）

1. 用户在活动列表页点击左缘把手 → 面板展开，页面右移 360px，列表仍完整可见。
2. 输入「帮我把桐庐旅行的预算改成 3 万」→ send（附 `page_path=/activities/`）→ 服务端上下文注入「用户正在浏览：活动列表页」。
3. AI 调 `activities.update` → 返回**预览卡**（不落库）；气泡渲染在侧边栏消息流。
4. 用户点确认 → hx-post apply → 服务端执行、成功分支构造 `page_actions: navigate /activities/<id>/`。
5. afterSwap 执行器 → 右侧跳到桐庐旅行详情页，预算已是 3 万；侧边栏靠 localStorage 自动重开 + 恢复同一会话历史。
6. 用户继续「再记一笔 200 的门票」→ send 附 `page_path=/activities/28/` → 上下文「正在浏览活动《桐庐旅行》」→ 预览 → 确认 → refresh（已在详情页）→ 页面自刷新出现新费用。

## 4. 改动清单

**服务端**（约 5 处）

- `chat/views.py`：① send 读 `page_path` → resolve 构造上下文行（过渡兼容旧字段）；② apply 成功分支新增 page_actions 构造函数（规则表见 D5）+ 片段渲染标记；③ 新端点 `GET /chat/sidebar/`（create-or-get 最近会话 + 历史片段 + 钉选挂载位，`widget_messages` 同款模式，`@json_login_required`）。
- `chat/urls.py`：挂 sidebar 路由。
- `core/agent_registry.py`：返回约定文档补 `page_actions` 可选字段（直执路径透传；apply 路径由 chat/views 构造）。
- 业务工具**零改动**（活动工具照旧返回）。

**前端**

- `templates/chat/partials/_sidebar_agent.html`（新）：把手 + 面板骨架，消息流/输入区复用既有 partial 与约定（textarea Enter 换行、`paFitTextarea`）。
- `base.html`：include 侧边栏 + 初始化脚本（localStorage 恢复、push margin 切换）+ afterSwap 执行器扩展。
- `static/js/sidebar-agent.js`（新）：面板开关、`PaChatTurn` 第四实例挂载、跳页恢复；执行器（afterSwap 消费 data-page-action）放 `chat-turn.js` 尾部 document 级委托区（与 local-card / status-fetch 委托同区，先例一致）。
- `custom.css`：`.side-agent` 系样式（Typeframe 令牌；新增类名要跑 Tailwind 构建）。

**测试**

- `chat/tests.py::SidebarAgentEndpointTest`（create-or-get / 越权 / 片段含 resume）、`PageContextInjectionTest`（resolve 命中与 miss、不污染消息历史、过渡兼容）、`PageActionTest`（apply 片段标记正确、URL 全部 reverse 生成、无 navigate 时不抑制 changed 提示条）。
- `ChatTurnFlowJsTest` 扩展执行器静态锁；`JsonEndpointNeverHtmxTest` 自动收编新端点；侧边栏模板接线锁（AskDrawerWiringTest 同款模式）；`DesktopLayoutCoverageTest` 的 SINGLE_COLUMN 名单不动（侧边栏是 fixed，不进两列体系）。

## 5. 安全与边界

- **不做「AI 自由操作 DOM / 事件模拟」**：页面反应只有三种声明式动作，全部服务端生成、URL 白名单（reverse + 同源校验）。
- page_actions 不进模型协议 → 无截断残骸面、无注入面。
- 钱包操作两步确认**不豁免**：侧边栏不提供免确认模式。
- 并发语义不变：单会话单轮 409 / `claim_turn` 防双执行，侧边栏与主对话页同会话天然受保护；页面卸载（navigate）时 halt 只停轮询，服务端轮次不受影响。
- 移动端完全不动（`md:` 以下不渲染）。

## 6. 分期

- **P1 骨架（用户例子的完整闭环）**：双栏 shell 布局改造（含 D2.1 牵连点）+ 把手/面板 + sidebar 端点 + page_path 上下文 + apply 后 navigate/refresh。验收场景：列表页对话改预算 → 右侧自动跳详情显示新值；详情页记费用 → 页面自刷新。
- **P2 打磨**：highlight 动作、「新话题」按钮、@ 钉选接入侧边栏、跳页恢复细节打磨、上下文覆盖扩展（日历/费用报告/记忆页）。
- **P3 议而不决**：移动端版本、页面正文按需注入、AI 主动跟随播报（右侧页面变化时侧边栏自动说话）。

## 7. 明确不做

- 不做流式打字机（平台 SSE 无 token 增量，既有结论）。
- 不做全局键盘快捷键（含 Esc 关闭）。
- 不做 iframe 壳 / SPA 化改造（push 模式 + 整页跳转已满足）。
- 不做「免确认写操作」「AI 直接改 DOM」。
