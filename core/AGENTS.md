# core/ — 横切能力层

> 横切规则（数据可见性、Agent 工具协议、Markdown 渲染、参与者写入等）见根 `AGENTS.md`。
> 本文件仅补充 **core 模块特有**的约定。

## 依赖方向

core 被所有业务 app 依赖，但 **core 绝不反向 import 业务 app**。
需要在函数内访问业务模型时，用延迟 import：

```python
def tag_suggestions(scope, user):
    from activities.models import Activity  # 延迟，不在模块顶部
```

`core/tags.py` 的 `_SCOPE_MODEL_PATHS` 字典用字符串路径 + `apps.get_model()` 做模型映射，
避免任何顶层跨 app 导入。

## 可见性四函数（`core/utils.py`）

`visible_qs` / `get_visible` / `visible_child_qs` / `get_visible_child` 是**唯一**合法入口。
新增视图或工具时：

- 有 `user` 字段的模型 → `visible_qs(Model, request.user)` / `get_visible(Model, request.user, id=...)`
- 无 `user` 字段的子模型（如 `Message` 挂在 `Conversation` 下）→ `visible_child_qs(Message, user, 'conversation')`
- **禁止** `Model.objects.all()`、**禁止**手写 `filter(user=request.user)` 或 `is_superuser` 分支

## Agent 工具注册（`core/agent_registry.py`）

各 app 在自己的 `agent_tools.py` 中用 `@agent_tool` 装饰器注册，`core.apps.ready()` 自动发现：

```python
@agent_tool('activities.query', description='按条件查询活动')
def query_activities(user, params):
    ...
```

- `INTENT_TOOL_MAP` 维护意图→工具名映射，`build_protocol_prompt()` 据此动态生成首帧协议
- 工具未注册时对应意图自动从协议中隐藏，不会报错
- `params_hint` 支持 callable：内容随数据库变化的工具（如标签建议）用 `lambda: tag_suggestions(...)` 实时求值
- 新增工具只需：① 在 app 的 `agent_tools.py` 写函数 + 装饰器 ② 在 `INTENT_TOOL_MAP` 加映射

## Markdown 渲染安全顺序（`core/markdown_render.py`）

`render_markdown()` 是全站唯一的 AI 文本渲染实现，通过 `ai_markdown` 模板过滤器暴露。
**安全顺序不可拆**：

```
escape(src) → 再插入自己生成的标签
```

- 入口第一件事是 `escape(src)`，模型输出的任何 HTML 只能以文字形态出现
- 链接再过一层 scheme 白名单（`_SAFE_URL` + `_UNSAFE_URL`），`javascript:` 伪链接退回普通文字
- **去掉 `escape` 就是全站 XSS**，`AiMarkdownRenderTest` 的攻击形态断言是唯一防线
- 用户消息（`role='user'`）**不走 Markdown**，保持 `whitespace-pre-wrap`

## 标签体系（`core/tags.py`）

自建 `core.Tag` 替代 django-taggit，`scope` 字段区分用途（`activity` / `expense` / `note` / `knowledge`）。
读写统一走本模块：`ensure_tags` / `apply_tags` / `tag_suggestions` / `used_tags`。
业务 app 不直接操作 `Tag.objects`。

## 共用工具函数

- `q_or(fields, term)`：全站唯一的 Q 拼接器（一个词跨多列 OR 模糊匹配）
- `char_overlap_ratio(a, b, mode)`：`symmetric` 双向相似 / `contains` 单向覆盖，语义不等价，勿合并
- `json_login_required`：`@login_required` 的 JSON 端点版，fetch 通道未登录时返 401 而非 302
- `daily_totals` / `pct_change`：图表与统计的通用计算，避免各页面各写一份
