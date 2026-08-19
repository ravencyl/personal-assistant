# Personal AI Assistant - 使用指南（本地运行）

本项目为个人使用，直接在本地运行即可，无需 Docker。

## 前置条件

- Python 3.12+
- Redis（可选，仅当需要 Celery 定时任务时安装）

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

# 7. 启动服务器
python manage.py runserver
```

访问 http://localhost:8000 即可使用。

## 日常启动

```bash
source venv/bin/activate
python manage.py runserver
```

## 可选：定时任务（Celery）

任务提醒、RSS 抓取等定时功能依赖 Redis + Celery。不需要时可直接跳过，不影响核心功能（对话、任务、知识库、书签）。

```bash
# 安装并启动 Redis（macOS）
brew install redis
brew services start redis

# 终端 1：Celery Worker
celery -A personal_assistant worker -l info

# 终端 2：Celery Beat（定时调度）
celery -A personal_assistant beat -l info
```

内置定时任务：

| 任务 | 频率 | 说明 |
|------|------|------|
| check_task_reminders | 每 5 分钟 | 检查即将到期的任务 |
| fetch_rss_feeds | 每 1 小时 | 抓取 RSS 订阅源 |
| cleanup_old_tasks | 每 24 小时 | 清理已完成的历史任务 |

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
4. 返回首页开始使用 AI 对话、任务管理等功能

## 管理命令

```bash
# 初始化预定义 Agent 到 Qoder 平台
python manage.py init_agents

# 仅预览，不实际创建
python manage.py init_agents --dry-run
```
