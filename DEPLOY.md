# 三磊 - 使用指南（本地运行）

本项目为个人使用，直接在本地运行即可，无需 Docker。

## 前置条件

- Python 3.12+
- Redis（生产环境缓存用，开发环境可选）

## 快速启动

```bash
cd 个人助手

# 1. 创建虚拟环境（首次）
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量：编辑 .env，填入 QODER_ACCESS_TOKEN 等

# 4. 数据库迁移（使用 SQLite，无需额外安装数据库）
python manage.py migrate

# 5. 创建管理员账户（首次）
python manage.py createsuperuser

# 6. 初始化预定义 Agent（需要有效的 QODER_ACCESS_TOKEN）
python manage.py init_agents

# 7.（可选）导入已有知识库文件
python manage.py import_knowledge_files

# 8. 启动服务器
python manage.py runserver
```

访问 http://localhost:8000 即可使用。

## 日常启动

```bash
source venv/bin/activate
python manage.py runserver
```

## 定时任务（cron）

项目使用系统 cron 执行定时任务（无需 Celery）。

### 配置方法

编辑 crontab：

```bash
crontab -e
```

添加以下条目（根据实际路径调整）：

```
# 每 30 分钟自动启动到期活动（planned → in_progress）
*/30 * * * * cd /path/to/个人助手 && source venv/bin/activate && python manage.py auto_start_activities >> /tmp/auto_start.log 2>&1

# 每日凌晨 2 点生成循环活动实例（未来 7 天）
0 2 * * * cd /path/to/个人助手 && source venv/bin/activate && python manage.py generate_recurring >> /tmp/generate_recurring.log 2>&1

# 每晚 21:30 生成每日晚间摘要（AI 优先，失败降级为规则模板，幂等可重跑）
30 21 * * * cd /path/to/个人助手 && source venv/bin/activate && python manage.py generate_daily_summary >> /tmp/daily_summary.log 2>&1

# 每早 06:30 生成每日个性化洞察（AI 优先，失败降级为规则模板，幂等可重跑）
30 6 * * * cd /path/to/个人助手 && source venv/bin/activate && python manage.py generate_daily_insights >> /tmp/daily_insights.log 2>&1
```

### 定时任务说明

| 命令 | 建议频率 | 说明 |
|------|----------|------|
| auto_start_activities | 每 30 分钟 | 将 start_date 已到的 planned 活动自动改为 in_progress |
| generate_recurring | 每日 1 次 | 根据循环活动规则生成未来 7 天的活动实例 |
| generate_daily_summary | 每晚 21:30 | 为活跃用户预生成当日摘要（今日回顾/消费/明日安排），展示在 Daily 页；当日已有摘要则跳过 |
| generate_daily_insights | 每早 06:30 | 为活跃用户预生成个性化洞察（结合记忆与行为数据），展示在 Daily 页建议区顶部；当日已有洞察则跳过 |

## 配置说明（.env）

| 变量 | 必填 | 说明 |
|------|------|------|
| SECRET_KEY | 否 | Django 密钥，本地使用默认值即可 |
| DEBUG | 否 | 默认 True |
| QODER_ACCESS_TOKEN | 是 | Qoder Cloud Agents API 令牌 |
| QODER_API_BASE_URL | 否 | API 地址，默认 https://api.qoder.com.cn/api/v1/cloud |
| QODER_DEFAULT_ENVIRONMENT_ID | 否 | 默认 Environment ID |
| REDIS_URL | 否 | Redis 地址，默认 redis://localhost:6379/0 |

## 首次使用

1. 访问 `/admin/` 登录 Django 管理面板
2. 进入 Agents > Agent 配置，确认 Agent 已同步
3. 进入 Agents > Environment 配置，设置默认 Environment
4. 返回首页开始使用
   - **今日**：每日简报 + AI 建议 + 习惯打卡
   - **活动记录**：活动管理 + 费用追踪 + 附件 + 模板
   - **AI 对话**：智能助手（支持查询/创建/修改活动、AA 分账、备忘录等）
   - **备忘**：快速记录与搜索
   - **知识库**：Markdown 文章管理与 AI 问答
   - **仪表盘**：周报/月报统计
   - **费用报告**：可视化图表分析

## 管理命令

```bash
# 初始化预定义 Agent 到 Qoder 平台
python manage.py init_agents

# 自动启动到期活动
python manage.py auto_start_activities

# 生成循环活动实例
python manage.py generate_recurring

# 导入 media/knowledge/ 下的 Markdown 文件为知识库文章
python manage.py import_knowledge_files

# 生成每日个性化洞察（AI 优先，失败降级，幂等）
python manage.py generate_daily_insights [--dry-run]
```

## 生产部署

生产环境使用 nginx + gunicorn 部署。

### gunicorn 注意事项

启动命令必须带 `--timeout 180`：

```bash
gunicorn --workers 3 --timeout 180 --bind unix:/var/www/personal-website/gunicorn.sock \
  personal_assistant.wsgi:application
```

理由：周报/月报、活动页的 AI 快速输入解析等仍在请求内同步调用 AI（`core/ai.py` 最长 `timeout=90`）。gunicorn 同步 worker 的看门狗只在请求间隙收到心跳，因此**只要单次 AI 调用超过 gunicorn timeout，worker 就被 SIGKILL**，用户看到 502、日志里出现 `WORKER TIMEOUT` + `SystemExit`（默认 30s 时实测 `GET /reports/weekly/` 要 30.3s，必被杀）。改完 unit 记得 `systemctl daemon-reload && systemctl restart gunicorn`。

**AI 对话不在这条链上**：`/chat/<id>/send/` 只做「落库 + 一次发起」则立即返回，等 AI 的循环被摊成 `turn_poll` 短轮询（每拍几十毫秒），所以一轮问答跑多久都不会撞看门狗；本轮上限由 `chat/models.py::TURN_TTL_SECONDS` 控制。

### 超时链口径（改任何一个都要同时看其余两个）

```
nginx proxy_read_timeout (180s)  ≥  gunicorn --timeout (180s)  >  请求内 ai_round_trip timeout（最大 90s）
```

外大内小：AI 真的跑满时应该是**应用层先超时并降级回一句普通回复**，而不是网关/看门狗把进程杀掉（后者用户只能看到 502）。报告类请求真的会走满 60-90s，这三个值不对齐时必现随机失败。`chat/tests.py::AiTimeoutChainTest` 会扫全仓库所有 `ai_round_trip(..., timeout=N)` 调用取最大值来对照本文的 `--timeout`，并单独断言聊天视图里没有 `wait_for_response` / `sleep`（防止同步阻塞长回来）。

### nginx 配置要点

- `/static/` 直接服务静态文件（含 manifest.json、sw.js）
- `/media/` 直接服务用户上传文件（附件、知识库文件）
- **`client_max_body_size` 必须 ≥ `settings.ATTACHMENT_MAX_UPLOAD_SIZE`（当前 10MB）**，线上取 `12m`：不设时 nginx 默认 1m，超过 1MB 的附件会在网关层直接返回 413（HTML 错误页，Django 那句「文件过大，上限 10MB」的友好提示根本没机会执行）；留 2MB 余量是为了让 Django 校验先触发
- **`proxy_read_timeout 180s`**（默认 60s）：AI 页面超过 60s 时 nginx 会先断开并返回 504，比 gunicorn 看门狗更早失败会把真实错因遮住
- 其余请求代理到 gunicorn

```nginx
client_max_body_size 12m;

location / {
    proxy_pass http://unix:/var/www/personal-website/gunicorn.sock;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 180s;
}

location /static/ {
    alias /path/to/个人助手/staticfiles/;
}

location /media/ {
    alias /path/to/个人助手/media/;
}
```

### 部署同步是增量的：服务器上会攒「本地已删除」的遗留文件

日常更新用的 `rsync -az`（不带 `--delete`）只推不删，所以本地删掉的模块在服务器上永远留着。2026-09-02 清点出 39 个这类文件：`content/`、`cms_pages/` 两个早已在 git 里删除的 app、它们的模板、`personal_assistant/celery.py`（Celery 已移除）、11 张 screenshot。危险的不是占地方，而是**它们在服务器侧的测试里真的会跑**：`SiteBrandTest` 扫 `templates/**/*.html`，被 5 个遗留模板（写死 `AI Assistant`、标题不含 `SITE_NAME`）打成常驻红灯，看起来像新改动搞坏了品牌口径。

清点漂移：本地 `git ls-files`（或直接 find 本地磁盘，排除 venv/.git/staticfiles/media/.qoder）与服务器同口径 find 结果做差集，remote-only 即漂移。

处理约定：**移到项目外的备份目录，不删**（`mv content /root/legacy-drift-<日期>/content`）。项目内遗留文件可能被 git 历史之外的东西引用，移动可以随时原路退回；确认站点 200 + 服务器全量测试绿了再谈彻底删除。

### 线上只读冒烟（不登录、不造数据）

生产环境验证「页面真的渲染出新结构」不必依赖浏览器：把脚本 scp 到服务器，`manage.py shell < script.py`，用 `django.test.Client` + `force_login` 打生产设置。三个坑必须先在脚本里进程内改掉，否则全是假阴性：

- `settings.SESSION_COOKIE_SECURE = False` / `CSRF_COOKIE_SECURE = False`：生产 Cookie 带 `Secure`，test client 走 http 内部请求，登录态存不住 → 每请求被 302，看起来像新模板没接上
- `settings.SECURE_SSL_REDIRECT = False`：否则 `SecurityMiddleware` 先把内部请求 301 掉
- `settings.ALLOWED_HOSTS += ['testserver']`：否则拿 400（`Invalid HTTP_HOST`），页面只有 143 字节错误页

红线：这类脚本只做读操作（GET 详情页、候选搜索、故意传不存在的 ID 看错误分支），钉选/发消息一律不真做；用完按 `session_key` 删掉自己那条 Session。真机交互验证另说（需要用户账号，不代填密码）。

### PWA 注意事项

Service Worker 和 manifest.json 位于 `/static/` 目录，需确保 nginx 正确 serve：

- `manifest.json` 的 Content-Type 应为 `application/manifest+json`
- `sw.js` 的 Content-Type 应为 `application/javascript`
- SW 的 `scope` 为 `/`，需确保从根路径可访问
