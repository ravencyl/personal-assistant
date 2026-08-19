# Personal AI Assistant - 部署指南

## 本地开发

### 前置条件
- Python 3.12+
- Redis (可选，开发环境可用 SQLite)

### 快速启动

```bash
# 1. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 QODER_ACCESS_TOKEN

# 4. 数据库迁移
python manage.py migrate

# 5. 创建管理员账户
python manage.py createsuperuser

# 6. 初始化预定义 Agent（需要有效的 QODER_ACCESS_TOKEN）
python manage.py init_agents

# 7. 启动开发服务器
python manage.py runserver
```

访问 http://localhost:8000 即可使用。

## Docker 部署

### 快速启动

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，设置：
#   - SECRET_KEY (随机字符串)
#   - QODER_ACCESS_TOKEN
#   - DEBUG=False (生产环境)
#   - ALLOWED_HOSTS (你的域名)

# 2. 启动所有服务
docker-compose up -d

# 3. 初始化数据库
docker-compose exec web python manage.py migrate

# 4. 创建管理员
docker-compose exec web python manage.py createsuperuser

# 5. 初始化 Agent
docker-compose exec web python manage.py init_agents

# 6. 收集静态文件
docker-compose exec web python manage.py collectstatic --noinput
```

### Docker Compose 服务说明

| 服务 | 说明 |
|------|------|
| web | Django + Gunicorn (端口 8000) |
| db | PostgreSQL 16 |
| redis | Redis 7 |
| celery | Celery Worker (异步任务) |
| celery-beat | Celery Beat (定时任务调度) |

### 数据卷

| 卷名 | 用途 |
|------|------|
| postgres_data | PostgreSQL 数据持久化 |
| static_data | 静态文件 |
| media_data | 用户上传文件 |

## 配置说明

### 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| SECRET_KEY | 是 | Django 密钥，生产环境必须更换 |
| DEBUG | 是 | 开发 True / 生产 False |
| ALLOWED_HOSTS | 生产 | 允许的域名，逗号分隔 |
| QODER_ACCESS_TOKEN | 是 | Qoder Cloud Agents API 令牌 |
| QODER_API_BASE_URL | 否 | API 地址，默认 https://api.qoder.com.cn/api/v1/cloud |
| QODER_DEFAULT_ENVIRONMENT_ID | 否 | 默认 Environment ID |
| REDIS_URL | 否 | Redis 地址，默认 redis://localhost:6379/0 |

## 首次使用

1. 访问 `/admin/` 登录管理面板
2. 进入 Agents > Agent 配置，确认 Agent 已同步
3. 进入 Agents > Environment 配置，设置默认 Environment
4. 返回首页开始使用 AI 对话、任务管理等功能

## 定时任务

系统内置以下定时任务（通过 Celery Beat 调度）：

| 任务 | 频率 | 说明 |
|------|------|------|
| check_task_reminders | 每 5 分钟 | 检查即将到期的任务 |
| fetch_rss_feeds | 每 1 小时 | 抓取 RSS 订阅源 |
| cleanup_old_tasks | 每 24 小时 | 清理已完成的历史任务 |

## 管理命令

```bash
# 初始化预定义 Agent 到 Qoder 平台
python manage.py init_agents

# 仅预览，不实际创建
python manage.py init_agents --dry-run
```
