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
```

### 定时任务说明

| 命令 | 建议频率 | 说明 |
|------|----------|------|
| auto_start_activities | 每 30 分钟 | 将 start_date 已到的 planned 活动自动改为 in_progress |
| generate_recurring | 每日 1 次 | 根据循环活动规则生成未来 7 天的活动实例 |

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
```

## 生产部署

生产环境使用 nginx + gunicorn 部署。

### nginx 配置要点

- `/static/` 直接服务静态文件（含 manifest.json、sw.js）
- `/media/` 直接服务用户上传文件（附件、知识库文件）
- 其余请求代理到 gunicorn

```nginx
location /static/ {
    alias /path/to/个人助手/static/;
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
