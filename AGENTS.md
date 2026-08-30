# 个人助手 — Agent 指引

Django 5.0+ 个人 Web 应用，集成活动管理、笔记、知识库、AI 对话与智能代理。

## 模块边界

```
personal_assistant/   根配置（settings、urls、wsgi）
core/                 横切能力层：报告生成、全局搜索、跨模块关联、提醒、建议
activities/           业务 app：活动 CRUD、费用、预算、模板、循环活动、附件
notes/                业务 app：快速备忘
knowledge/            业务 app：Markdown 知识库文章 + AI 问答
chat/                 对话层：会话管理、消息收发、AI 卡片渲染
agents/               代理层：Qoder Cloud Agent 配置同步、Environment 管理
```

**依赖方向单向**：`core` 被其他 app 调用，但 `core` 不反向依赖业务 app（除 `Reminder → Activity/Message` 的 FK 外）。`chat` 和 `agents` 仅消费 `core` 提供的编排能力。

**路由挂载**：所有 app 路由通过 `personal_assistant/urls.py` 统一 include，各 app 内部维护自己的 `urls.py`。

## 数据可见性规则

所有业务数据按用户隔离，通过 `core/utils.py` 统一接入：

```python
from core.utils import visible_qs, get_visible

# 列表查询：超级用户见全部，普通用户仅见自己的
qs = visible_qs(Activity, request.user)

# 单对象获取：不存在或无权时返回 404
activity = get_visible(Activity, request.user, id=activity_id)
```

- `is_superuser=True`：可见全部用户数据，可编辑/删除
- 普通用户：仅可见 `user=request.user` 的数据
- **所有视图必须使用 `visible_qs` 或 `get_visible`**，禁止直接 `Model.objects.all()`
- 子活动继承父活动的归属（`user` 不变）

## Agent 工具协议

工具注册在 `core/agent_registry.py`，各 app 在自己的 `agent_tools.py` 中用 `@agent_tool` 装饰器注册：

```python
from core.agent_registry import agent_tool, ToolError, CandidateToolError

@agent_tool('activities.query', description='按条件查询活动')
def query_activities(user, params):
    # 业务校验失败 → 抛 ToolError，编排器转为 ⚠️ 友好提示
    if not params.get('target'):
        raise ToolError('请告诉我搜索关键词')

    # 目标不唯一 → 抛 CandidateToolError 让用户选择
    if matches.count() > 1:
        raise CandidateToolError('找到多个匹配', candidates=[...])

    # 成功返回 dict
    return {'reply': '找到 3 个活动', 'card': 'activity_list',
            'activity_ids': [...], 'changed': False}
```

**返回约定**：`{'reply': str, 'card': str|None, 'activity_ids': [], 'card_data': {}, 'changed': bool, 'list_url': str, 'action': {}}`

**错误分类**（`ChatOrchestrator.process` 统一捕获）：

| 异常 | 行为 |
|------|------|
| `CandidateToolError` | 渲染候选卡片，`logger.info` |
| `ToolError` | 拼接 `⚠️ {message}` 回复，`logger.warning` |
| `Exception` | 返回固定文案「操作失败，请稍后重试」，`logger.error` |

**容错铁律**：任何环节失败都降级为普通文本回复，绝不阻断对话。非核心操作（日志写入、知识库注入等）失败仅 `logger.warning`。

**写操作必须记录日志**：所有活动写操作调用 `log_activity(user, activity, action, summary)`。

## 参与者写入规则

参与者一律通过 `activities/utils.py` 的 `resolve_participants(user, names, create_missing=False)` 写入，禁止 `Participant.objects.get_or_create`：

```python
# AI 自动识别路径（对话 create/update、快速输入、一句话子任务）：只匹配已有，匹配不到就跳过
participants, skipped, _created = resolve_participants(user, names)

# 用户显式填写路径（内联手动表单、活动编辑页）：先大小写不敏感复用已有写法，确实没有才新建
participants, _skipped, created = resolve_participants(user, names, create_missing=True)
```

- 匹配键为 `name.strip().lower()`（同时忽略 `@` 前缀），因此手输 `yyx` 会归到已存在的 `YYX`，不再产生大小写变体重复联系人
- **AI 路径绝不自动新建联系人**；`skipped` 非空时必须在 `reply` / JSON `note` 中告知用户「哪个名字没加」，不得静默丢弃
- `activities.update` 全未命中时保持原参与者不变（「未找到」不等于「清空」），预览卡片与实际写库口径必须一致
- 历史遗留重复用 `python manage.py merge_participants`（默认 dry-run，加 `--apply` 才合并删除，保留 `created_at` 最早的一条）；写法不同的同人（如 `Joe` → `Joe Yan`）用 `--map "别名:保留名"` 显式合并，保留名不存在直接报错，绝不静默新建

## 前端约定

- **HTMX 局部渲染**：搜索面板（`base.html`）、聊天消息（`conversation_detail.html`）、习惯打卡（`daily.html`）、确认动作（`_confirm_actions.html`）通过 `hx-post` + `hx-target` + `hx-swap` 实现无刷新交互
- **模板结构**：`templates/` 下按 app 分目录（`templates/activities/`、`templates/chat/` 等），继承 `base.html`
- **样式**：Tailwind CSS v4 + CSS 变量（`--text`、`--bg`、`--border` 等），响应式布局（移动端堆叠 + 桌面端横排）
- **静态文件**：手写 CSS 在 `static/css/`，PWA 资源（`manifest.json`、`sw.js`）在 `static/` 根目录
- **django-htmx 中间件**：已全局启用，视图可用 `request.htmx` 判断是否为 HTMX 请求

## 前端分端约定（移动端 / 桌面端）

- **唯一分端断点**：`md:`（768px，与导航层一致）。结构性显隐只用成对块：桌面元素 `hidden md:flex` / `hidden md:block`，移动元素 `md:hidden`。禁止新增 `sm:` 结构性断点；`sm:p-*` / `sm:text-*` / `sm:gap-*` 等纯尺寸渐进类不属于结构，保留。640-768px 平板竖屏跟随桌面布局（预期行为）。
- **双协议约定**：UI 局部更新走 HTMX + HTML 片段端点；返回 JSON 的数据端点必须由原生 `fetch()` 消费，**严禁在元素上挂 `hx-*`**（HTMX 会把 JSON 当纯文本插入 DOM；混用还会与 fetch 竞争导致渲染失效）。fetch 的 CSRF token 从 `base.html` 的 `<meta name="csrf-token">` 读取。
- **禁止手动 `htmx.process()`**：htmx 内置 MutationObserver 会自动初始化新增节点，手动重复处理会造成双重绑定与旧节点引用残留（曾引发聊天浮窗 `r is not a function` 错误）。
- **分端工具类**（见 `static/css/custom.css`）：`.tap-target`（移动端最小 44×44 触控区）、`.daily-card-focus`（键盘选中描边态）、`.hover-actions`（桌面随 `.group` 悬停显示，触屏/移动端常驻可见）。

## 配置

通过 `django-environ` 读取项目根目录 `.env` 文件：

```python
import environ
env = environ.Env(DEBUG=(bool, True))
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY', default='django-insecure-dev-key-change-in-production')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])
QODER_ACCESS_TOKEN = env('QODER_ACCESS_TOKEN', default='')
QODER_API_BASE_URL = env('QODER_API_BASE_URL', default='https://api.qoder.com.cn/api/v1/cloud')
REDIS_URL = env('REDIS_URL', default='redis://localhost:6379/0')
```

**环境差异**：
- `DEBUG=True`（开发）：LocMemCache 缓存
- `DEBUG=False`（生产）：RedisCache + `SECURE_PROXY_SSL_HEADER` + 安全 Cookie

## 修改 activities/views.py 前须知

1. **可见性过滤**：所有查询通过 `visible_qs(Activity, request.user)` 或 `get_visible(Activity, request.user, ...)` 过滤，导入自 `core.utils`
2. **渲染目标**：视图返回标准 Django `render()` / `JsonResponse()`，HTMX 交互由模板层 `hx-*` 属性控制（主要在 `activity_list.html` 的筛选表单和列表区域）
3. **装饰器顺序**：`@login_required` → `@ensure_csrf_cookie`（如需）→ `@require_POST`（写操作）
4. **标签建议**：`_user_tag_names(request.user)` 返回可见范围内已使用过的标签，供表单 autocomplete
5. **快速输入**：`parse_quick_input_view` 先调 AI（Qoder agent），失败降级为 `parsing.parse_quick_input` 规则解析

## 默认发布流程（长期规则，无需向用户确认）

**每次代码改动完成并通过验证后，自动连续执行「提交 Git → 推送远程 → 部署生产 → 线上验证」四步。**

1. **前置条件**：本次改动自测通过（`python manage.py test` 相关模块全绿 + `python manage.py check` 无 issue）才进入提交流程；验证失败先修复再走流程，禁止提交坏代码。
2. **提交 Git**：`git add -A` + 语义化 commit message（`feat/fix/chore(scope): 中文摘要`，正文列出关键变更点）。
3. **推送远程**：`git push origin main`；网络超时失败时保留本地提交并在汇报中明确提示「待手动 push」，不静默跳过。
4. **部署生产**：按 deploy-personal-assistant skill 既定流程——rsync 同步代码（排除 venv/.git/.env/db.sqlite3/media/staticfiles/.qoder*）→ 有新迁移时服务器执行 `migrate` → `collectstatic --noinput` → `systemctl restart gunicorn` → `is-active` 确认。
5. **线上验证**：curl 检查受影响页面/端点状态码符合预期（登录 200、鉴权页 302、POST 无 CSRF 403 等），必要时查 gunicorn 日志确认无新异常。
6. **结果汇报**：一次性给出 commit hash、push 状态、部署步骤结果、线上验证结论；无迁移时说明「本次无需 migrate」。

**例外（仍需先询问用户）**：数据库结构破坏性变更、改动生产 `.env`、清理/覆盖生产数据、任何不可逆的服务器操作。

## 常用命令

```bash
python manage.py runserver              # 启动开发服务器
python manage.py migrate                # 数据库迁移
python manage.py init_agents            # 同步预定义 Agent 到 Qoder 平台
python manage.py auto_start_activities  # 自动启动到期活动（planned → in_progress）
python manage.py generate_recurring     # 生成未来 7 天循环活动实例
python manage.py import_knowledge_files # 导入 media/knowledge/ 下的 Markdown 文件
python manage.py test                   # 运行全部测试
python manage.py test core activities   # 运行指定模块测试
```
