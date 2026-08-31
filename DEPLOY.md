# Personal AI Assistant - 使用指南（本地运行）

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

启动命令必须带 `--timeout 120`（或任何大于最长 AI 调用超时的值）：

```bash
gunicorn --workers 3 --timeout 120 --bind unix:/var/www/personal-website/gunicorn.sock \
  personal_assistant.wsgi:application
```

理由：周报/月报、每日洞察等页面在请求内同步调用 AI（`core/ai.py` 最长 `timeout=90`）。gunicorn 同步 worker 的看门狗只在请求间隙收到心跳，因此**只要单次 AI 调用超过 gunicorn timeout，worker 就被 SIGKILL**，用户看到 502、日志里出现 `WORKER TIMEOUT` + `SystemExit`（默认 timeout 是 30s，实测 `GET /reports/weekly/` 要 30s 左右，必被杀）。改完 unit 记得 `systemctl daemon-reload && systemctl restart gunicorn`。

### nginx 配置要点

- `/static/` 直接服务静态文件（含 manifest.json、sw.js）
- `/media/` 直接服务用户上传文件（附件、知识库文件）
- **`client_max_body_size` 必须 ≥ `settings.ATTACHMENT_MAX_UPLOAD_SIZE`（当前 10MB）**，线上取 `12m`：不设时 nginx 默认 1m，超过 1MB 的附件会在网关层直接返回 413（HTML 错误页，Django 那句「文件过大，上限 10MB」的友好提示根本没机会执行）；留 2MB 余量是为了让 Django 校验先触发
- 其余请求代理到 gunicorn

```nginx
client_max_body_size 12m;

location /static/ {
    alias /path/to/个人助手/staticfiles/;
}

location /media/ {
    alias /path/to/个人助手/media/;
}
```

### PWA 注意事项

Service Worker 和 manifest.json 位于 `/static/` 目录，需确保 nginx 正确 serve：

- `manifest.json` 的 Content-Type 应为 `application/manifest+json`
- `sw.js` 的 Content-Type 应为 `application/javascript`
- SW 的 `scope` 为 `/`，需确保从根路径可访问
