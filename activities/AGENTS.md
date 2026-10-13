# activities/ — 活动与费用管理

> 横切规则（数据可见性、Agent 工具协议、Markdown 渲染、参与者写入等）见根 `AGENTS.md`。
> 本文件仅补充 **activities 模块特有**的约定。

## 模型关系

```
Activity (user FK)
├── children (self FK, parent→self, SET_NULL)   子活动继承父活动 user
├── expenses (Expense, activity FK)              一活动对多费用
├── tags (M2M → core.Tag, scope='activity')
├── participants (M2M → Participant, user FK)
├── attachments (Attachment, activity FK)
└── logs (ActivityLog, activity FK)              操作时间线
```

- **子活动继承父活动归属**：`parent.user` 不变，子活动的 `user` 字段在创建时由视图显式设为与父活动一致
- **费用关联方式**：`Expense.activity` 是 FK（`related_name='expenses'`），`Expense.user` 独立标记归属用户
- **费用标签**：原 `category` 字符串字段已迁移至 `core.Tag`（`scope='expense'`），通过 `Expense.tags` M2M 关联

## 参与者写入（`activities/utils.py`）

`resolve_participants(user, names, create_missing)` 是**唯一**参与者写入入口，禁止 `Participant.objects.get_or_create`：

| 路径 | `create_missing` | 行为 |
|------|:-:|------|
| AI 自动识别（对话 create/update、快速输入、一句话子任务） | `False` | 只匹配已有，匹配不到跳过并在 reply 中告知 |
| 用户显式填写（内联手动表单、编辑页） | `True` | 先大小写不敏感复用已有写法，确实没有才新建 |

- 匹配键：`name.strip().lower()`，同时忽略 `@` 前缀 → 手输 `yyx` 归到已有 `YYX`
- AI 路径**绝不自动新建联系人**；`skipped` 非空时必须在回复中告知
- `activities.update` 全未命中时保持原参与者不变（「未找到」≠「清空」）

## 费用标签建议（`core/tags.py`）

原 `ExpenseCategory` 模型与 `categories.py` 缓存已随 migration 0017 退役，
费用类别现在走 `core.Tag(scope='expense')`。获取建议标签：

```python
from core.tags import tag_suggestions
suggestions = tag_suggestions('expense', request.user)  # 活跃标签 ∪ 用户用过的（含停用）
```

视图层通过 `_user_tag_names(request.user)` 和 `tag_suggestions('expense', ...)` 向前端传递可选标签。

## 「日常开支」归属桶

无活动语境的费用归入系统桶活动（`DAILY_BUCKET_NAME = '日常开支'`），
识别靠 `daily_bucket_q()`（标题 + 描述 marker 双重条件），不是简单按名字匹配。
用户自建同名活动不会被静默收养成系统桶。

## 写操作日志

所有活动写操作调用 `log_activity(user, activity, action, summary)`：
- 失败仅 `logger.warning`，不影响主流程（容错铁律）
- `fmt_field('description', ...)` 截断到 40 字，防止长文本撑爆时间线
- `edit_summary(old, activity)` 对比编辑前后快照生成变更摘要

## 活动筛选（`activities/utils.py`）

`filter_activities(user, params)` 是列表视图与 Agent 查询工具的共用筛选入口，
支持 `status` / `tag` / `date_from` / `date_to` / `name` / `participant` / `keyword`，
非法值静默忽略。排序与分页由各自视图自取，不属于筛选职责。

## 已下线功能（不要以「有用」为由加回来）

| 功能 | 下线时间 | 说明 |
|------|----------|------|
| 活动模板 / 循环活动 | 2026-09 | 模型、视图、路由、cron 全删 |
| 列表多选批量操作 | 2026-09-23 | 使用率低；对话里的 `activities.batch_status` 不受影响 |
| 列表「笔数 / 子活动」列 | 2026-09-23 | 费用格的「含子活动」标注保留 |
| 前置依赖 `blocked_by` + 依赖关系图 | 2026-10-13 | 用户决定，含数据一并删除（M2M 表已 drop） |
| 活动评论 `ActivityComment` | 2026-10-13 | 用户决定，含数据一并删除 |

前置依赖与评论的删除范围：模型字段/模型、`is_blocked`·`blocking_names`·`would_create_cycle`、
表单 `blocked_by` 字段与 `save_blocked_by`、`blocked_search/add/remove` 与
`activity_comment_add/delete` 五个路由、`dependency_data` 路由与 `dependency_views.py`、
`static/js/dependency-graph.js`、Agent 工具 `activities.comments`·`activities.add_comment`·
`activities.check_blocked` 及 `INTENT_TOOL_MAP` 的三个意图、卡片快照的
`comments_count`·`comments`·`add_comment_url`·`is_blocked`·`blocking_names`、
Daily 的 `_annotate_blocked` 与「等待前置」徽标、详情页评论卡与前置依赖卡。

**唯一有意保留的残留**：`ActivityLog.ACTION_CHOICES` 里的 `('commented', '评论了活动')`。
线上仍有 9 条历史评论日志，删掉 choice 会让详情页时间线把这些行渲染成空白；
它现在纯展示，没有任何写入路径。

防复活锁：`activities/tests.py::DependencyAndCommentRemovedTest`（字段/路由/工具/模板/源码五组静态断言）。
