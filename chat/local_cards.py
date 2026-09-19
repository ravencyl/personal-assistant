"""非 AI 快捷卡片注册表：「点击 → 服务端直出卡片」的通用机制。

设计目标（交互重构 2026-09-19）：「用对话的方式用全 app，但不为每句话付 token」。
凡是服务端自己就能查出来、不需要智能的数据（今日概览、费用统计……），都注册成
一张本地卡片：前端点击按钮 → POST /chat/<id>/local-card/ → gather(user) 纯服务端
查询 → 落一条 role='system' 的消息（payload 走既有的卡片协议）→ 渲染进消息流。

三条硬边界：
- **绝不调用云端 Agent**：不碰 get_service / turn 状态机，不耗 token，不算一轮 turn。
  消息历史也天然不进 AI 上下文（_build_ai_content 只组装 turn_prompt，消息历史根本
  不发给平台；_referenceable_reply 只取 assistant，system 角色自动被引用池排除）。
- **快照式 payload**：gather 返回的 dict 整份存进 Message.payload.card_data，历史
  渲染只读快照、不回查数据库 —— 点按钮那一刻的数据长什么样，回看就是什么样。
- **失败静默降级**：gather 抛异常由端点转成 JSON 错误提示（不落消息、不阻断对话），
  前端显示一条瞬态提示就完事。
"""
import logging

logger = logging.getLogger(__name__)

# key → {'key', 'label', 'gather'}。gather(user) -> dict（card_data 快照）。
_REGISTRY = {}


def register_local_card(key, label, gather):
    """注册一张本地快捷卡片。重复注册以最后一次为准（测试里方便覆写）。"""
    _REGISTRY[key] = {'key': key, 'label': label, 'gather': gather}


def get_local_card(key):
    """按 key 取卡片定义；未注册返回 None（端点据此回 400）。"""
    if not key:
        return None
    return _REGISTRY.get(key)


def _register_defaults():
    """内置卡片。gather 一律函数内延迟导入：core/activities 是其他 app，
    让 import 发生在首次真正取卡片时，避免模块加载顺序耦合。"""
    from activities.views.daily_views import daily_brief_payload
    from core.views import today_brief_payload

    register_local_card('daily_brief', 'daily', daily_brief_payload)
    register_local_card('today_brief', '工作台', today_brief_payload)


_register_defaults()
