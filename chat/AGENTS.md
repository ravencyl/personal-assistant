# chat/ — 对话与异步收发

> 横切规则（数据可见性、Agent 工具协议、Markdown 渲染等）见根 `AGENTS.md`。
> 本文件仅补充 **chat 模块特有**的约定。

## 异步 turn 状态机

一个 Qoder session 同时只能处理一轮（发第二条拿 409），状态全部落库：

```
none → queued → awaiting → finalizing → done
                ↑ 409 回退          ↘ 超时/发送失败 → error
```

| 状态 | 含义 |
|------|------|
| `none` | 无进行中的轮次 |
| `queued` | 用户消息已存，尚未确认发到 Qoder（撞 409 回到此状态） |
| `awaiting` | 已发到 Qoder，等 assistant 文本 |
| `finalizing` | 某个轮询已认领收尾：跑编排器 + 落库 |
| `done` | 本轮已落库 |
| `error` | 超时 / 用户取消 |

- `POST /chat/<id>/send/` **只做「落库 + 一次发起」并立即返回**
- 等 AI 的循环搬到 `GET /chat/<id>/turn/`（轮询端点，每拍一次无副作用状态读取）
- 收尾（跑编排器 + 落库）在轮询请求里完成

## claim_turn 并发保护

`Conversation.claim_turn(from_state, to_state)` 是**条件 UPDATE**，不是普通赋值：

```python
updated = Conversation.objects.filter(
    id=self.id, turn_state=from_state,
).update(turn_state=to_state)
```

两个标签页都从 `awaiting` 拿到同一段文本时，抢不到锁的必须空手而回，
否则会落两条一模一样的 assistant 消息并把写操作执行两次。
SQLite 下单行 UPDATE 自带原子性，不需要额外锁。

## 前端唯一实现（`static/js/chat-turn.js`）

分栏页与详情页**共用同一份** `PaChatTurn(...)`，不要写第二套：

- 等回复期间**只锁发送按钮、输入框保持可编辑**（能接着打字是异步化的目的）
- 切对话调 `halt()`（只停轮询），`stop()` 才是取消服务端本轮
- 三个 JSON 端点（send/turn/cancel）一律原生 `fetch`，**严禁 `hx-*`**
- fetch **必须带 `Accept: application/json`**：视图靠它区分 fetch 与无 JS 表单提交

## 协议 JSON 与透传分支的分流

`ChatOrchestrator.process()` 按回复内容分流两类消息：

| 类型 | 判据 | 处理 |
|------|------|------|
| 操作站点数据 | `looks_like_protocol(text)`（以 `{` 开头且含 `"intent"`） | 解析 JSON → `INTENT_TOOL_MAP` 分发工具 |
| 通用问答 | 不是协议 JSON | 自然语言透传（模型已自己调联网工具查完） |

- **协议逃生舱**：`looks_like_protocol` 为 true 但解析不出完整 JSON 时，
  先尝试 `salvage_partial_protocol` 抢救已闭合的 `params`；救不出则返 `PROTOCOL_TRUNCATED_NOTE`
- **透传不等于原文照搬**：`extract_follow_ups` 会从透传分支也剥出「下一步」chips
- `ask` 意图故意不注册工具 → `tool_name` 为空 → 原样把 `reply` 给用户

## 占位文案与引用安全

取消/超时/空回复都落一条 assistant 消息，这些文案**会落库**：

```python
PLACEHOLDER_REPLIES = {TURN_EMPTY_NOTE, TURN_CANCELLED_NOTE, TURN_TIMEOUT_NOTE,
                       TURN_INTERRUPTED_NOTE, PROTOCOL_TRUNCATED_NOTE, TOOL_FAILURE_REPLY}
```

- `$LAST_REPLY` 引用必须走 `_referenceable_reply`，跳过 `PLACEHOLDER_REPLIES` 与 `looks_like_protocol`
- 补发判据与引用池**必须共用同一个函数**，否则会出现「提醒用 $LAST_REPLY，展开出来却是另一条」
- **新增占位文案时必须登记进 `PLACEHOLDER_REPLIES`**

## 超时链

`nginx proxy_read_timeout(180s) ≥ gunicorn --timeout(180s) > chat.AI_WAIT_TIMEOUT(90s)`
聊天不再在这条链上（`TURN_TTL_SECONDS` 只是浏览器轮询预算）。
`AiTimeoutChainTest` 会扫全仓库取最大值对照 `DEPLOY.md`。
