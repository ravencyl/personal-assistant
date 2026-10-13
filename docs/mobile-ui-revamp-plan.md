# 移动端 UI Revamp 整改清单（2026-10-13）

用户定性：当前移动端 UI 10/100 分，「不像应用，像 IT 系统」。三条审计线（字号 / 圆角密度 / 建活动点击流）的汇总与分批实施计划。

## 一、诊断摘要

### 字号：同角色最多 4 档并存（48 个模板约 800 处 text-*）

| 角色 | 现存变体 | 证据示例 |
|---|---|---|
| 页面主标题 h1 | text-lg / text-xl / text-2xl / text-2xl sm:text-3xl | conversation_list:23 (lg) vs dashboard:16 (2xl) vs expense_report:14 (2xl→3xl) |
| 卡片标题 h2 | text-sm font-semibold / text-base / text-lg / text-xs font-bold | dashboard:65（规范）vs article_list:66 (lg) vs conversation_list:130 (xs bold) |
| 徽章/元信息 | text-xs / text-[10px] / text-[11px] + custom.css 0.7rem（第四套口径） | _status_badge:3 vs today_brief_card:36 vs stats_card:44 |
| 列表行主文字 | text-[15px] vs text-sm | activity_list:296、_daily_card:35 |
| 输入框 | 16px（iOS 防缩放，保留）但周围全是 text-xs → 视觉突兀 | custom.css:615 |

### 圆角：模板 5 档、运行时只认 CSS 一档胶囊（最严重的债）

- `custom.css:166` 有 `button/.btn/input/select → border-radius:999px !important`，导致模板里 42 处 `rounded-md` 按钮全部**失效**，源码与真机观感脱节。
- 同文件 :162 注释写「胶囊满屏是模板感主因」，值却是 999px——2026-09-13 与 2026-09-15 两代口径都没清。
- 一级卡三个值：`.app-card` 20px（基类）vs expense_report 6 处手拼 12px vs 建活动弹窗 16px vs _daily_card 手拼 `rounded-[1.25rem]` 20px。
- 二级面：chat 卡 12px、.note-item 16px、气泡 8px 三种并存。
- 单行输入 999px 胶囊 vs textarea 16px——同表单形状不一。

### 密度：最挤 Top 5

1. activity_detail（text-xs ×57、py-2 ×19、费用/安排区 text-xs+mt-0.5 叠加）
2. conversation_list（text-xs ×22 + text-[10px] ×4、py-1 ×4）
3. activity_list 移动卡（px-3 py-2.5 + text-xs + gap-1.5 三连）
4. memory_list（py-1 ×4、唯一 text-[11px]）
5. note_list（py-0.5 残留 + .note-item 0.875rem）

Tap target：`py-1`/`py-1.5` 可点元素约 10 处（实际高 32-36px），`.tap-target` 只覆盖 chat 卡与两个列表页，未成全站习惯。

### 建活动：理论 2 tap，实际 3 tap + 整页刷新 + 键盘遮按钮

- 弹窗表单本身不重（名称唯一必填），成本在：① 日期不预填今天；② 快速通道「解析→确认」两步且确认后整页 reload；③ 快记面板默认停在「备忘」Tab 每次多切一下；④ 提交按钮不 sticky，键盘弹出必被遮。

## 二、统一 token 方案

### 字号 7 档（收敛后全站只允许这些档）

| Token | 类 | 用途 |
|---|---|---|
| display | text-2xl sm:text-3xl | 报告/登录大标题 |
| title | text-xl | 页面 h1（全部统一到这一档） |
| card-title | text-sm font-semibold | 卡片 h2 |
| body | text-base | 正文、输入框文字 |
| secondary | text-sm | 列表主行、按钮、label |
| caption | text-xs | 元信息、badge、表格、底栏 |
| micro | text-[10px] | 仅 ID 角标等极小字（text-[11px]/0.7rem 并入） |

配套：输入框保持 16px（iOS 红线），但周边正文从 text-xs 上提到 text-sm/text-base，输入框不再突兀；custom.css 的 0.7rem/0.75rem/0.875rem 散落值全部对齐档位。

### 圆角 4 档

| Token | 值 | 用途 |
|---|---|---|
| R-pill | 999px | 徽章、chip、状态点、头像 |
| R-ctl | 12px | 按钮、输入框、select、textarea（删除 999px !important 覆盖，让模板类重新生效） |
| R-surface | 16px | 卡内二级面、弹窗、气泡、note-item |
| R-card | 20px | 页面一级卡（.app-card/.panel） |

### 间距 3 档

- 列表行统一 `px-4 py-3`；卡内统一 `p-4`（紧凑区 `p-3`）；space-y 只留 2/4/6。
- 可点元素高度 <44px 的一律挂 `.tap-target`。

## 三、分批实施计划

| 批 | 内容 | 改动面 |
|---|---|---|
| **B1 token 基建** | custom.css：删 999px !important、定 4 档圆角变量、字号散点对齐；改共享局部 `_status_badge`/`_daily_card`/chat cards 族（text-[10px]/[11px] 归一） | ~6 文件，全站观感立即变 |
| **B2 重灾区页面** | activity_detail、conversation_list、activity_list、memory_list、note_list 按新档位重排字号/间距/tap target | ~5 文件 |
| **B3 h1/h2 全站统一** | 20 个 h1 收敛到 text-xl、20+ 个 h2 收敛到 text-sm font-semibold；expense_report 手拼卡改挂 .app-card | ~20 文件（机械替换） |
| **B4 建活动提速** | ① 日期预填今天、结束=开始；② 弹窗打开即聚焦名称；③ 回车=解析+就地可编辑预览+主按钮确认（保留写操作确认口径）；④ 成功后局部插卡不整页刷新；⑤ 快记面板记忆上次 Tab；⑥ 提交按钮 sticky 底栏 | 弹窗 + quick-parse.js + forms.py |
| **B5 验收与锁** | 浏览器 390px 实测全页面截图对比；core/tests.py 加静态锁（禁 text-[11px]/rounded-md 复活、h1 档位断言）；npm build:css + 四步上线 | 测试文件 |

验收口径：同一角色全站一个字号；按钮/输入框全站一个圆角；tap target ≥44px；建活动 ≤3 tap 且键盘不遮提交；Tailwind 构建通过 + 全量测试绿 + 真机截图复验。

## 四、待拍板的三个方向

1. **控件圆角**：保留胶囊（999px，现状）vs 收敛为 12px 圆角（推荐：App 感更强，审计已认定胶囊是「模板感主因」）。
2. **建活动提速**：B4 全做（推荐）vs 只做默认值预填（最小改动）。
3. **字号整体上提**：按上表上提（列表/正文 text-sm/text-base，辅助 text-xs，推荐）vs 只统一档位不上提大小。
