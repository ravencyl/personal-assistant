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

# 子模型（自身没有 user 字段，如 Message → Conversation.user）
qs = visible_child_qs(Message, request.user, 'conversation')
msg = get_visible_child(Message, request.user, 'conversation', id=message_id)

# JSON 端点需要 404 错误体时（返回 (obj, response)，二者只有一个非 None）
obj, resp = get_visible_or_json(ActivityTemplate, request.user, id=template_id)
if resp is not None:
    return resp
```

- `is_superuser=True`：可见全部用户数据，可编辑/删除
- 普通用户：仅可见 `user=request.user` 的数据
- **所有视图必须使用上面四个函数之一**，禁止 `Model.objects.all()`、禁止手写 `filter(..., user=request.user)` / `is_superuser` 分支做权限判断（无权时统一 404，不用 403）
- 子活动继承父活动的归属（`user` 不变）

### 两个例外口径（有意为之，不是漏改）

按“这堆数据是谁的”划分，而不是按“在哪个 app”划分：

| 口径 | 适用 | 写法 |
|------|------|------|
| 个人指标 | 花费合计、预算消耗等“我花了多少”的统计数字 | `filter(user=request.user)`（超管也不混他人数据） |
| 个人上下文 | 记忆注入/AI 检索、提醒、每日摘要、建议生成 | `filter(user=<当前用户>)`，`memory/services.py` 有说明 |
| 可见范围 | 列表页、日历、Daily 的活动分区、全局搜索、模板、笔记/文章/记忆页面 | `visible_qs` / `get_visible` |

新增检索入口必须复用 `core.utils.q_or(fields, term)`（一个词跨多列 OR）与 `knowledge.utils.tokenize`；相似度判定复用 `core.utils.char_overlap_ratio(a, b, mode=...)`（`symmetric` 双向相似、`contains` 单向覆盖，两者不等价，勿合并）。

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

## 对话能力的两类分流（改首帧协议前必读）

云端 Agent（Qoder）本身就带着联网工具，**能不能用全看我们的 prompt 让不让用**。首帧协议把每条消息分成两类：

| 类型 | 输出形式 | 谁处理 |
|------|----------|--------|
| （1）操作站点数据 | `{"intent", "params", "reply"}` JSON | 编排器分发 `@agent_tool` |
| （2）通用问答（需要外部/最新信息） | **自然语言直答**（模型自己先调 WebSearch/WebFetch） | 编排器「非 JSON 透传」分支 |

`ask` 是（2）类的显式意图，**故意不注册工具**（`INTENT_TOOL_MAP` 里没有），`tool_name` 为空 → 原样把 `reply` 给用户，系统不再动作。

- **新能力默认要注册成工具走（1）类**；只有“系统确实不需要动作”时才归到（2）类，否则会被误伤（`knowledge.search` 抢答通用问题导致只会回“没有找到”就是这么来的）
- 工具返回的 `reply` **直接给用户看、不会再送回模型**：文案只写口语结论，不得夹带对模型的指令或提示词片段
- `knowledge.search` 只能查用户自己存的文章，**不得当通用问答的兜底**；本地未命中不得以“没有找到”结尾，要引导到联网
- 对话默认绑的 Agent 由 `chat/views.py::CHAT_AGENT_PURPOSE` 控制（当前 `knowledge`：平台上手工配过、工具集含 WebSearch/WebFetch/ImageSearch 且 `always_allow`）。给对话换 Agent 改这个常量，**不要靠 `.first()` 的排序碰运气**
- 超时链：`nginx proxy_read_timeout(180s) ≥ gunicorn --timeout(180s) > chat.AI_WAIT_TIMEOUT(90s)`；改完要同步 `DEPLOY.md`，`AiTimeoutChainTest` 会拿文档里的值做断言

### 把对话结论落库（写工具的口径）

用户说“把刚才那段结论存下来”时，有两个出口（模型看得到整个 session，所以正文汇总它自己干，服务端只落库）：

| 工具 | 意图 | 适用 |
|------|------|------|
| `knowledge.create` | `knowledge_create` | 成体系的资料、清单、总结（落 `Article`，回复给文章链接） |
| `activities.update` + `description` | `update` | 只属于某个活动的备注、结论、待定项 |

- **覆盖型写入必须显式声明**：`activities.update` 的 `description` **默认追加**到原描述末尾，整段替换要传 `description_mode="replace"`。因为模型看不到活动原描述全文，给它一个默认覆盖等于给了一个“一句话冲掉用户长文本”的按钮（预览卡上追加会写明“保留原文”）
- 创建类工具（`*.create`）**立即生效**，不出确认卡；`update` / `delete` 这类会改或毁已有数据的必须走“预览 → `apply_fn` 确认执行”两步流（见 `activities` 的 P1 区）
- 描述变更要进 `ActivityLog`，所以 `fmt_field('description', ...)` **截断到 40 字**；新增长文本字段上日志同理，整段贴进时间线会爆布局
- AI 回复在模板里是**纯文本**（`{{ msg.content }}`，既不走 markdown 也不走 urlize），所以给用户的链接要 `unquote()` 成可读路径（`knowledge/agent_tools.py::_article_url`），否则中文 slug 会变成一串 `%E7%BE%8E...`
- 正文类入参（`content`）要有下限校验（太短直接 `ToolError` 让模型补），否则存进去一堆“详见上文”的碎片，后续也查不出来

回归锁：`chat/tests.py`（协议逃生舱、透传路径、含 `{}` 的自然语言不得被误判为协议 JSON、超时链）、`core/tests.py::AgentRegistryConsistencyTest`（意图指向未注册工具会静默失效）、`knowledge/tests.py`、`activities/tests.py::UpdateDescriptionAgentToolTest`。

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
- **名字相似不构成合并依据**：只有用户明确确认“是同一个人”才能写 `--map`。已确认的反例：线上 `id=28「Joey」` 与 `id=9「Joe Yan」` 是两个不同的人，不得处理

## 提醒状态口径

`core.models.Reminder.status`：`pending`（待触发）→ `fired`（已触发但用户未处理）→ `done`（用户确认做完），任何阶段可 `dismissed`（忽略）。

- 自动触发只写 `fired`（`check_due_reminders`，仅改 `pending`，不覆盖用户手动设置的状态）
- Daily「提醒」区与浮窗红点只取 **fired**；`done`/`dismissed` 属于已处理，不得再出现
- 新增状态取值时同步 `core/reminder_tools.py` 的白名单（现在直接读 `STATUS_CHOICES`）与 `ReminderDoneStatusTest`

## 前端约定

- **HTMX 局部渲染**：搜索面板（`base.html`）、聊天消息（`conversation_detail.html`）、习惯打卡（`daily.html`）、确认动作（`_confirm_actions.html`）通过 `hx-post` + `hx-target` + `hx-swap` 实现无刷新交互
- **模板结构**：`templates/` 下按 app 分目录（`templates/activities/`、`templates/chat/` 等），继承 `base.html`
- **上下文键不得占用框架保留名**：视图 context 里禁用 `messages`（`django.contrib.messages` 的 flash 变量，`base.html` 顶部提示条在读它）、`user`、`perms`、`request`。曾把 Message queryset 放进 `messages`，基模板就按 `Message.__str__`（「[role] 正文前 50 字」）把整段对话历史渲染到页面顶部，看着像调试信息泄漏，同时 flash 提示静默失效；现在用 `chat_messages`，`chat/tests.py::ConversationDetailContextTest` 会扫模板兼断言
- **样式**：Tailwind CSS v4 + CSS 变量（`--text`、`--bg`、`--border` 等），响应式布局（移动端堆叠 + 桌面端横排）
- **状态色单一来源**：活动状态的配色只写在 `static/css/custom.css` 的 `--status-<status>` 变量里，模板用 `.status-bg--`（圆点/色条/日历块）/ `.status-fg--`（图标）/ `.status-fg-on--`（色块上的文字）/ `.badge-status--`（徽章）+ `{{ activity.status }}`，**禁止再写按状态分支的 `bg-zinc-*` / `text-zinc-*`**，也禁止在视图/接口里下发 hex（曾有 4 份映射，done 与 cancelled 深浅互为颠倒；`activities/tests.py::StatusDisplaySingleSourceTest` 会守住这一点）
- **静态文件**：手写 CSS 在 `static/css/`，PWA 资源（`manifest.json`、`sw.js`）在 `static/` 根目录（但 `sw.js` 对外必须从站点根 `/sw.js` 注册，见下面两条）
- **AI 回复是纯文本渲染**：`{{ msg.content }}`（`whitespace-pre-wrap`，既不走 markdown 也不走 urlize），所以工具 `reply` 里给用户的链接要 `unquote()` 成可读路径（`knowledge/agent_tools.py::_article_url`），中文 slug 的编码 URL 用户读不了也点不了
- **对话输入框 Enter 不提交**：两个入口（`chat/conversation_detail.html` 的 `#message-input`、`base.html` 浮窗的 `#chat-input`）都是 `<textarea rows="1" data-auto-grow>`，Enter 只换行、**只能点「发送」按钮**（单行 input 在表单里会隐式提交，而误发一条要等 AI 几十秒）；高度由 `base.html` 末尾的 `window.paFitTextarea` 统一接管（程序清空后要再调它重置，内联 `hx-on::after-request` 已这么写），`.chat-input` 负责 `resize:none` + 160px 限高内滚动。该脚本必须保留「元素未渲染就跳过」的守卫：浮窗面板初始 `display:none`，此时 `scrollHeight` 恒为 0，无条件回写会把输入框压成 0 高、placeholder 裁字（已踩过），面板展开时由 `showChatView` 补拟合；`chat/tests.py::ChatInputEnterBehaviorTest` 锁住这套结构
- **站点品牌名只有一个口径**：`settings.SITE_NAME`（当前为「三磊」），模板一律写 `{{ SITE_NAME }}`（`core.context_processors.site_brand` 全局注入），**禁止在模板/视图/JS 里硬写站名**；浏览器标签页标题统一「页面名 · {{ SITE_NAME }}」。Admin 标题、AI 自我介绍 prompt 都读同一个 settings。例外：`static/manifest.json`（PWA 安装名）是静态文件、不走模板引擎，**改名时必须手动同步**，`core/tests.py::SiteBrandTest` 会断言它与 settings 一致
- **Service Worker 只在站点根上注册，脚本本体仍住在 `static/sw.js`**：`base.html` 注册 `{% url 'service_worker' %}` = `/sw.js`，由 `core/views.py::service_worker` 用 `finders.find('sw.js')` 直出（`require_safe` + `Cache-Control: no-store` + `Service-Worker-Allowed: /`），不为它单改 nginx。**SW 可控作用域的上限就是脚本 URL 所在目录**，注册回 `/static/sw.js` 就管不到任何页面导航，脚本里的离线降级分支直接变死代码。装饰器得是 `require_safe` 而不是 `require_GET`：后者把 HEAD 拒成 405，而 `curl -I /sw.js` 正是发布后验响应头的手段。`core/tests.py::ServiceWorkerTest` 锁住这套结构
- **`static/sw.js` 的三条拦截约束 + 两条回填时序约束**（拦截顺序本身被测试锁定）：❶ **永不拦自己**：`/sw.js`、`/static/sw.js` 要在任何 `respondWith` 之前 return，旧版把脚本归进 `/static/` cache-first 分支，浏览器每次更新检查拿回的都是缓存里的旧脚本，升 `CACHE_VERSION` 也不生效 —— 「发布后样式滞后一次」的真因；❷ 只处理 GET（表单 POST 的 `request.mode` 也是 `navigate`，缓存它等于允许重放写操作，而且 `cache.put()` 遇非 GET 抛异常）；❸ 只缓存顶层导航，判据只能用 `mode === 'navigate'`，**不能用「Accept 含 text/html」**（HTMX 片段请求正带这个头，会被当成页面缓存掉）；❹ `response.clone()` 必须在把响应交给 `respondWith` 之前同步做完（放进 `caches.open().then()` 里时原响应已开始往页面流，`clone()` 抛 `Response body is already used`）；❺ `cache.put` 必须挂在 `event.waitUntil()` 上（即发即忘会在写入前被回收）。❹❺ 都是静默失败：表现为缓存里永远只有 `PRECACHE_URLS`，离线兜底形同虚设（两条都在本地实测踩过，`test_runtime_refill_survives_body_handoff` 锁住）
- **本地 CSS/JS 必须用 `{% staticv %}` 引用，不得写裸 `{% static 'css/…' %}` / `{% static 'js/…' %}`**：`core/templatetags/core_tags.py::staticv` 给 URL 带内容哈希（`?v=<10位md5>`，按文件 mtime+size 缓存哈希，改文件自动换号，不用重启开发服务）。**为什么必需**：nginx 的 `location /static/` 不下发任何 `Cache-Control`（只有 etag / last-modified），浏览器按「启发式新鲜度」（约 `(now - Last-Modified) × 10%`）直接复用磁盘里的旧文件，连网络都不走；SW 的 network-first 用的就是这条 `fetch()`，同样被 HTTP 缓存答回来 —— 所以「走了网络优先」并不等于「拿到了新文件」。真实故障：全站两列化上线后，用户看到活动详情页「快捷操作」掉到页底（新 HTML + 旧 CSS，`.page-cols` 无 grid 声明就退化成块级流）。找不到源文件时 `staticv` 退回裸 URL，不阻断渲染；用了 `staticv` 的模板顶部必须 `{% load static core_tags %}`（漏了是整页 500）。`core/tests.py::StaticAssetVersionTest` 锁住这四条
- **改 CSS/JS 仍不需要手动升 `CACHE_VERSION`**（号由 `staticv` 自动带）：`/static/css/`、`/static/js/` 走 network-first，断网才回退缓存；cache-first 只留给体积稳定的图标/manifest；`/media/` 直连网络不缓存（用户上传同名覆盖后不该看到旧文件）。只有改 precache 清单或改缓存键方案才升版本号。另：静态资源的**缓存键必须剥掉查询串**（`staticKey = new Request(url.origin + url.pathname)`），因为 `PRECACHE_URLS` 写的是裸路径而模板现在带 `?v=`；不剥不会报错，只会默默 miss，断网时页面能开但完全没样式。页面导航相反必须保留查询串（`?page=2` / `?tag=x` 是不同内容）。`ServiceWorkerTest` 两个方向都锁住了
- **`base.html` 里清理旧窄作用域注册的迁移代码必须枚举后按 `scope` 筛**（`getRegistrations()` + `reg.scope.indexOf('/static/') !== -1`）；**禁止 `getRegistration('/static/').unregister()`**，那个 API 返回的是「覆盖给定 URL 的最长作用域注册」，遗留注册清干净后它就是根注册本身，等于每次开页自删根注册（实测因此整条离线兜底失效）
- **桌面端默认左右两列，列宽只由 `.page-cols` 决定**：除少数天然单列页（下表）外，页面在 md+ 都必须是「左列 = 主内容流、右列 = 辅助信息/概览/操作入口」。实现只有一份：`custom.css` 里 `@media (min-width: 768px)` 下的 `.page-cols`（`minmax(0,1fr) 320px` + `gap 2rem` + `align-items:start`）、`.page-main`、`.page-rail`（整列 sticky + 列内滚动）。**禁止页面另抄一份 grid、禁止模板内联 `grid-template-columns`**（`DesktopLayoutCoverageTest` 守这两条）。模板里两个列容器要写成对的可读锚点：`<div class="page-main">…</div><!-- /左列 -->`、`<div class="page-rail">…</div><!-- /右列 -->`，收尾 `</div><!-- /.page-cols -->`（锁靠这三个注释切列，比按嵌套深度猜可靠）
- **移动端阅读顺序靠 DOM 顺序守，不靠显隐类**：列容器永远不加 `hidden`/`md:flex` 之类的结构类，这样 <768px 下它们是普通块，视觉顺序 = DOM 顺序。需要「右列内容在移动端先出现」时（搜索/筛选/概览要先于列表），把右列整块写在前面并加修饰类 `page-cols--rail-first`（桌面靠 `order` 换回右侧）；锁会断言修饰类与 DOM 顺序一致，两者脱钩就是左右颠倒
- **阅读体不跟着列宽跑**：两列化后左列有 864px，文章正文/报告正文的 `prose` 必须自己限宽（`max-w-2xl`），否则一行超 100 字。长表单也不进右列（口径：**筛选/搜索进右列，多字段表单留左列**），320px 栏里的输入框只有 280px 实际宽度。条件渲染的右列块要有无条件兄弟块兜底，否则桌面端整列空着
- **刻意保持单列的页面（改之前先看这张表，`DesktopLayoutCoverageTest.SINGLE_COLUMN` 钉死了名单）**：`activities/activity_list.html`（宽表 `min-w-[760px]`）、`activities/activity_calendar.html`（7 列网格，压进左列每格不足 123px）、`activities/next_actions.html`（两组等权重，用等分 `md:grid-cols-2`）、`activities/activity_form.html` 与 `knowledge/article_form.html`（纯表单页）、`chat/conversation_detail.html`（单一线性时间轴）、`registration/login.html`（居中构图 + 全站唯一品牌位）
- **布局锁的位置**：共享断言件在 `core/layout_asserts.py::assert_desktop_two_columns(case, html, template_src=, left=, right=, mobile_order=, rail_first=)`，一次查完「列容器唯一成对 / 列归属 / 移动端 DOM 顺序 / 列声明只在 768px / 无 `sm:`与`lg:`结构断点」。它只读文本、不 import 业务 app（横切层才能被六个 app 共用）。**静态扫类名时必须走 `code_only(src)` 剔注释**：那些「为什么改」的注释里就会提到旧类名，拿散文当代码会假失败（已踩两次）
- **品牌图不手写**：全部由 `brand/make_logo_assets.py` 从 `brand/raven-sanlei-source.png` 裁切生成（跑法见脚本首行；pillow/numpy 装在 `/tmp/pil-lib`，**不进 requirements.txt**）。产出位置固定：`static/img/logo-mark.png`（导航，`h-7`）、`static/img/logo-lockup.png`（登录页完整标，`w-40`）、`static/icons/{favicon-16,32,48,apple-touch-icon,icon-192,icon-512,icon-maskable-512}.png`。换标只换源图重跑脚本，**不要手改 PNG**；资源尺寸按「页面显示宽 × 2」给，大了就是白多几倍流量。方形图标统一「深色底板 + 反白加粗图形」（细线标缩到 48px 以下不断线没有第二条路），加粗量按目标像素换算见 `TARGET_THICKEN` / `NAV_MARK_THICKEN` 注释；`core/tests.py::BrandAssetTest` 会验证 manifest 与模板引用的每个图标文件真实存在（旧 `icon.svg` 占位图就是漏网案例，已删）。`brand/explorations/` 是落选的字形探索稿（「三磊」六套方案 + 预览页），不参与生成、不入引用，**不要手改也不要往里放生产资源**
- **django-htmx 中间件**：已全局启用，视图可用 `request.htmx` 判断是否为 HTMX 请求

## 前端分端约定（移动端 / 桌面端）

- **唯一分端断点**：`md:`（768px，与导航层一致）。结构性显隐只用成对块：桌面元素 `hidden md:flex` / `hidden md:block`，移动元素 `md:hidden`。禁止新增 `sm:` 结构性断点；`sm:p-*` / `sm:text-*` / `sm:gap-*` 等纯尺寸渐进类不属于结构，保留。640-768px 平板竖屏跟随桌面布局（预期行为）。
- **双协议约定**：UI 局部更新走 HTMX + HTML 片段端点；返回 JSON 的数据端点必须由原生 `fetch()` 消费，**严禁在元素上挂 `hx-*`**（HTMX 会把 JSON 当纯文本插入 DOM；混用还会与 fetch 竞争导致渲染失效）。fetch 的 CSRF token 从 `base.html` 的 `<meta name="csrf-token">` 读取。
- **禁止手动 `htmx.process()`**：htmx 内置 MutationObserver 会自动初始化新增节点，手动重复处理会造成双重绑定与旧节点引用残留（曾引发聊天浮窗 `r is not a function` 错误）。
- **分端工具类**（见 `static/css/custom.css`）：`.tap-target`（移动端最小 44×44 触控区）、`.hover-actions`（桌面随 `.group` 悬停显示，触屏/移动端常驻可见）。
- **详情页两列具体内容分配**：`activity_detail.html` 用通用列容器（类名口径见上面【前端约定】的 `.page-cols` 条）包住左列（描述·费用·子任务）与右列（快捷操作卡·附件·参与者·关联）。右列整块 DOM 排在左列之后，所以移动端相对旧版只有一处变化：子任务从页底提到费用之后。改这个模板的块顺序就是改移动端顺序，必须同步 `ActivityDetailDesktopLayoutTest` 的顺序锁。
- **Daily 页两列具体内容分配**：`daily.html` 是 **rail-first**（右列整块 DOM 排在左列之前：今日概览 = 打卡与提醒·关键数字·今日进度·今日摘要·本周消费），桌面端靠 `page-cols--rail-first` 的 `order` 换到右侧，因此移动端阅读顺序与改造前逐块一致。sticky 列有高度预算（`max-height: calc(100vh - 5.5rem)`）：往 `.page-rail` 里加卡片要算总高，超了会出现列内滚动并裁掉底部卡片（靠压缩内容解决，别改 `max-height`）；rail 顶层子块间距统一 `md:mb-6`。三个次要长列表（今日进行中 / 即将开始 / 最近完成）**桌面端默认折叠**，靠 `window.matchMedia('(min-width: 768px)')` 门控默认值，移动端仍默认展开 —— 新增折叠分区要同时进 restore 脚本的 `sections` 数组，且禁止另写一套折叠逻辑（复用 `toggleSection()` + `localStorage['daily_section_'+id]`）。「今日进度」卡上的习惯完成率靠 `htmx:afterSwap` 监听 + `[data-habit-row]` 重算，改打卡表单的 `hx-swap` 会静默失效。改这个模板的块顺序/显隐要同步 `DailyDesktopLayoutTest` 的顺序锁与进度卡锁。
- **禁用全局键盘快捷键**：用户因误触（尤其 AI 对话时）已要求移除全部键盘快捷键（Daily J/K/D/I/P/X/E、Cmd+K 搜索、Esc 关闭、搜索结果方向键导航等）。表单内的 Enter 提交仍属正常行为（但 AI 对话输入框已按上一条约定改成 Enter 只换行、只能点按钮发送）；新增功能禁止再挂 `document` 级 `keydown` 全局监听。

## 配置

通过 `django-environ` 读取项目根目录 `.env` 文件：

```python
import environ
env = environ.Env(DEBUG=(bool, True))
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY', default='django-insecure-dev-key-change-in-production')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])
SITE_NAME = env('SITE_NAME', default='三磊')   # 站名；可在 .env 覆盖，无需改代码
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
5. **快速输入**：`parse_quick_input_view` 先调 AI（Qoder agent），失败降级为 `parsing.parse_quick_input` 规则解析。两条路径的相对日期口径必须一致（含昨天/前天/大前天/N天前/上周X 这类**往回看**的说法，补记的花费要落在花钱那天）：改一边要同步 `_week_anchor_text` 与 `RelativeDateParsingTest`
6. **写路径走 `activities/services.py`**：创建活动（含子任务）与记费用一律调 `create_activity_from_parsed` / `add_expense`，视图与 Agent 工具只做取参、鉴权、渲染；禁止在视图/工具里直接 `Activity.objects.create` 或 `Expense.objects.create`

### 写入口径（services 已统一，新增入口不得偏离）

| 项 | 口径 |
|------|------|
| 金额 | `clean_amount` → `Decimal` 两位小数（禁 `float`）；空值不记这笔；「记一笔」入口传 `positive=True` 拒 0 元 |
| 已花的钱 vs 预算 | cost → `Expense`；budget → `Activity.budget`，不得互写 |
| 消费日期 | 创建：未传/空/非法 → 今天；`clear_date=True` → 留空（仅 AA 分账等派生记录）；编辑：空 → 真清空 |
| 类别 | `clean_category` 同时接受英文 key 与中文显示名（反查 `Expense.CATEGORY_CHOICES`，不另存别名表），识别不了 → `other` |
| 校验失败 | services 抛 `InputError`；视图转 400 + 同文案，Agent 工具转 `ToolError` |
| 子任务归属 | `user` 继承父活动所有者（超管建的下级仍归原主人）；日志固定两条：父 `sub_created` + 子 `created` |
| 花费 vs 预算字段 | 一句话解析出的 cost 走 `record_parsed_cost`：空/0/非法静默跳过，绝不阻断创建 |

## 默认发布流程（长期规则，无需向用户确认）

**每次代码改动完成并通过验证后，自动连续执行「提交 Git → 推送远程 → 部署生产 → 线上验证」四步。**

1. **前置条件**：本次改动自测通过（`python manage.py test` 相关模块全绿 + `python manage.py check` 无 issue）才进入提交流程；验证失败先修复再走流程，禁止提交坏代码。
2. **提交 Git**：`git add -A` + 语义化 commit message（`feat/fix/chore(scope): 中文摘要`，正文列出关键变更点）。
3. **推送远程**：`git push origin main`；网络超时失败时保留本地提交并在汇报中明确提示「待手动 push」，不静默跳过。
4. **部署生产**：按 deploy-personal-assistant skill 既定流程——rsync 同步代码（排除 venv/.git/.env/db.sqlite3/media/staticfiles/.qoder*）→ 有新迁移时服务器执行 `migrate` → `collectstatic --noinput` → `systemctl restart gunicorn` → `is-active` 确认。
5. **线上验证**：curl 检查受影响页面/端点状态码符合预期（登录 200、鉴权页 302、POST 无 CSRF 403 等），必要时查 gunicorn 日志确认无新异常。
6. **结果汇报**：一次性给出 commit hash、push 状态、部署步骤结果、线上验证结论；无迁移时说明「本次无需 migrate」。

**例外（仍需先询问用户）**：数据库结构破坏性变更、改动生产 `.env`、清理/覆盖生产数据、任何不可逆的服务器操作。

## 运维专用端点（无 UI 入口，不得当死代码删除）

| 端点 | 方法/权限 | 用途 |
|------|-----------|------|
| `/api/agents/status/` | GET · `staff_member_required` | 人工 curl 确认 Qoder Cloud API 连通性与当前 base_url |
| `/api/agents/sync/` | POST · `staff_member_required` | 把平台侧 Agent / Environment 回灌到本地 `AgentConfig`/`EnvironmentConfig`（平台改过名字或版本后手工同步） |

两者均无模板/JS/cron 调用者，改动页面前不会命中，但删掉会打破运维习惯。新增同类端点时请一并在本表里登记并写清调用方。

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
