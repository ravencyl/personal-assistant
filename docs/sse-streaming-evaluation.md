# SSE 流式回复方案评估

日期：2026-10-11 · 状态：待拍板 · 结论先行：**不建议一步到位做端到端 SSE，建议「长轮询收口 + gthread」两步走（Phase 1），真 SSE 中继（Phase 2）挂起等平台支持 token 增量**。

---

## 一、实测事实（本轮在生产服务器上对平台 API 直测，两次取证）

测试方式：用生产 token 建临时会话实测 `GET /sessions/{id}/events/stream`，测完即归档清理。

| # | 事实 | 证据 | 对方案的意义 |
|---|------|------|--------------|
| F1 | **`agent.message` 整条一次性到达，无 token 级增量**。600 字请求的回复是 1 个事件、单 text 块 489 字符；17.4 秒生成期间流上零输出 | probe2 事件时刻表：`t=2.17 user.message` → `t=19.59 agent.message[489]` | 「逐字打字机」效果平台给不了。SSE 卖点直接折损一半 |
| F2 | 流是**实时**的：本轮开始（`session.status_running`、`user.message`、`span.model_request_start`）与结束（`agent.message`、`session.status_idle`、`session.usage`）都在事件发生瞬间推达 | probe2 全序列 | 用它可以把「回复完成的感知延迟」从现在的最坏 1.5s（轮询间隔）压到 ~100ms |
| F3 | **不回放历史**：对 idle 存量会话连 6 秒收 0 事件；带 `Last-Event-ID` 重连同样收 0（文档宣称的断点续传实测无效，至少 idle 后不给） | probe1 ①② | 断线不能靠重连补事件，**收尾落库仍必须以 REST 读事件为准**（现有 `poll_turn` 的 desc 窗口逻辑不动），流只能当「通知器」 |
| F4 | idle 时流不自动关闭（挂住连接） | probe1 ①（6s 主动掐断） | 每条打开的流占一个上游连接 + 一个我们的 worker 槽位，生命周期必须自己管 |
| F5 | 事件 id 为 `evt_*`，`content` 为块数组（`{type:'text',text}`），与 REST `/events` 同构 | probe1/2 | 解析层可与 `extract_latest_reply` 共用，改造面小 |

**推论**：F1+F3 决定了端到端 SSE 现在只能买到「更快收口 + 过程进度」，买不到「逐字流」。而这两样用更便宜的形状就能拿到。

## 二、现状架构与延迟构成

```
浏览器 chat-turn.js ──(每1.5s GET turn/)──> Django(sync worker) ──(REST GET events desc)──> Qoder 平台
```

- 回复完成的感知延迟 = 轮询间隔（最坏 1.5s）+ 一次 REST 往返（~200ms）≈ **最坏 1.7s**。
- 真正的用户痛点是**模型生成本身的 5~225s**（云端推理 + 联网研究），这一段任何前端方案都省不掉；SSE 能优化的只是首尾那 ~2 秒和「过程中的可见性」。
- worker：gunicorn `--workers 3 --timeout 180`（sync）；nginx `location /` 未显式设 `proxy_buffering`（默认 on，会缓冲 SSE，需 `X-Accel-Buffering: no` 响应头或独立 location 关闭）。

## 三、候选方案对比

| 方案 | 形状 | 得到的收益 | 代价/风险 | 判定 |
|------|------|-----------|----------|------|
| **A. 长轮询收口（推荐）** | 新增 `GET turn/wait/`：服务端在请求内开一条**短命 SSE**（≤25s），收到本轮 `status_idle` 立即关流、走现有 REST 收尾落库并返回；超时则返回 processing 由前端再开一拍。前端把 1.5s 轮询改成「长轮询循环」 | 完成感知延迟 1.7s→~0.3s；改动集中在一个端点 + 前端循环壳；**收尾协议、claim_turn、模板全不动** | 每个等待中的请求占住一个 sync worker ≤25s——3 worker 下两人同时聊天就饿死全站 → **必须先换 gthread**（见 Phase 1.1） | ✅ Phase 1 |
| **B. 端到端 SSE 中继** | 浏览器直连我们的 `/turn/stream/`，服务端把平台流原样转发（含 `status_running`、`span.*` 进度事件） | 在 A 之上多「过程进度」（模型开始推理/工具在跑）；未来平台若上 token 增量可白捡打字机 | worker 模型必须异步化（gthread 勉强 / uvicorn+ASGI 更合理）；nginx 要加 `proxy_buffering off` 专用 location；断线语义、双标签页、TTL 状态机都要重审；**而 F1 决定今天它没有打字机可推** | ⏸ 挂起，盯平台 changelog |
| **C. 纯前端假流式** | 拿到整条回复后前端逐字渲染 | 零后端改动，「看起来」在流 | 自欺：等待时长没变，只是把 489 字播 3 秒；违背「回复即结论」的诚实口径 | ❌ 不做 |
| D. 维持现状 + 已上线的秒数反馈 | — | 已上线（a90fb1a：首拍 500ms + 「已等 N 秒」） | 感知延迟仍最坏 1.7s，无过程进度 | 底线，不满足用户诉求 |

## 四、推荐落地：Phase 1（两步，均可独立回滚）

**1.1 worker 模型换 gthread（半天，含观察期）**
- `gunicorn --workers 3 --threads 8 --timeout 180`（或 `gthread` worker class 显式声明）。
- 影响面：全站并发槽位 3→24；SQLite 单写者模型下 8 线程并发写会撞 `database is locked`？——现有写点都在毫秒级事务，风险低，但需 `timeout('busy', 5000)` 兜底（settings `DATABASES OPTIONS` 加 `timeout`，实测本地默认 5s 已生效则不动）。
- 回滚：改回一行 ExecStart。
- 验证：`ab`/curl 并发打 Daily 与列表页，重启后观察 gunicorn 日志无 WORKER TIMEOUT/锁异常。

**1.2 长轮询收口端点（1~1.5 天，含测试）**
- `agents/services.py` 加 `wait_turn_finished(session_id, budget)`：httpx `stream('GET', '/events/stream')`，读到本轮 `session.status_idle` / `agent.message` 即返回（read timeout=预算），异常/超时返回 None。**只当通知器，不解析正文**（F3：正文永远以 REST desc 窗口为准，复用现有 `poll_turn`）。
- `chat/views.py` 加 `GET /chat/<id>/turn-wait/`：要求轮次活跃（否则直接回 done 现状）；调 `wait_turn_finished(≤25s)`，收到信号→立即执行现有 `turn_poll` 的收尾分支并返回；没收到→返回 `{'state':'processing','phase':...}`。复用 claim_turn 语义，不新开第二套收尾路径。
- 前端 `chat-turn.js`：循环从 `setTimeout(tick, 1500)` 改为「发一次 turn 拍（即时状态）→ 再挂 turn-wait → 拿到结果继续」；页面隐藏（`visibilitychange`）时退回 1.5s 普通轮询，避免后台 tab 挂连接。
- 进度反馈顺带升级：`wait` 期间服务端已能看到 `span.model_request_start`（=「模型正在思考」）、`tool_use` 类事件（=「正在查资料/执行操作」），phase 文案表比现在的 5 档更准——这是用户「等得安心」的实际来源。
- 测试锁：FakeQoderService 增加 `wait_turn_finished` 替身（沿用「不提供 wait_for_response」的防回退思路）；断言 turn-wait 不产生第二份落库、与双标签页 claim 兼容；nginx 侧无改动（响应是普通 JSON，不涉及缓冲）。
- 回滚：前端循环切回纯 1.5s 轮询即可，端点留着无人调。

**Phase 1 后预期**：快轮（5~15s 简单问答）总感知提升约 1.5~2s + 进度粒度变细；慢轮（研究型 77~225s）从「干等数字跳动」变成「模型思考中→查资料中→落地中」的真实阶段。

## 五、风险与边界

- **worker 占用**：长轮询每拍最多挂 25s；单用户场景 1~2 个并发完全够，但要给端点加「同一 conversation 已有等待者」的软闸门（turn_state 记 `waiting_poll` 或简单用 claim 语义），防双标签页各挂一条。
- **平台侧未文档化的限制**：并发流数、单流最长时长未知（F4 说明它不主动掐）。预算 ≤25s 刻意小于 nginx/gunicorn 的 180s，撞上限的形态是「我们的读超时先到」，可控。
- **Last-Event-ID 不可依赖（F3）**：任何「靠重连补事件」的设计都不许写。
- **超时链不变**：nginx(180) ≥ gunicorn(180) > 请求内最长等待(25s+收尾)，`AiTimeoutChainTest` 口径继续成立；DEPLOY.md 若有 90s 描述需同步。
- **AGENTS.md 双协议条**：turn-wait 仍是 JSON 端点 + 原生 fetch，禁 hx-*，走 `apiFetch`。

## 六、成本汇总

| 项 | 工作量 | 风险 |
|----|--------|------|
| Phase 1.1 gthread | 半天（含 24h 观察） | 低（一行回滚） |
| Phase 1.2 长轮询收口 + 进度事件 | 1.5~2 天（含回归锁） | 中低（收尾路径复用，不新开） |
| Phase 2 端到端 SSE（若平台日后支持 token 增量） | 3~5 天 + ASGI 专项 | 高（worker 模型/网关/状态机全动）——**今天不做** |

## 附：取证原始数据

- probe1（存量会话）：idle 首连 6s → 0 事件；Last-Event-ID 重连 6s → 0 事件；idle 不自动关流。
- probe2（新会话，600 字请求）：`2.17s session.status_running / user.message / span.model_request_start` → `19.59s agent.message(单块 489 字符) / span.model_request_end / session.status_idle / session.usage`，同刻成组到达。
- 脚本：`/tmp/pa_sse_probe.py`、`/tmp/pa_sse_probe2.py`（服务器 /tmp 已清理会话，脚本本地留存备重跑）。
