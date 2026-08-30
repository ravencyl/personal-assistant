"""与云端 Agent 单次往返交互的公共能力。

快速输入解析（activities.views._ai_parse）、报告生成（core.report_generator）、
每日洞察与记忆抽取都走这里，避免各处自己拼 create_session → send → wait。

约定：本模块不吞异常（除“配置缺失返回 None”），降级策略由调用方决定，
符合 AGENTS.md「任何环节失败都降级、绝不阻断主流程」。
"""
import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json_dict(text):
    """从 AI 回复中提取首个 JSON 对象（兼容前后有说明文字/代码块），失败返回 None"""
    m = re.search(r'\{.*\}', text or '', re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def ai_round_trip(prompt, timeout=60, purpose=None):
    """公共 AI 会话往返：create_session → send_message → wait_for_response → finally cancel_session。

    返回 AI 回复文本（可能为空串）；无可用 Agent/Environment 配置时返回 None。
    purpose 指定时优先选该用途的 Agent（如 general），否则取任一启用 Agent。
    异常向上抛出，由调用方捕获后降级处理。
    """
    from agents.models import AgentConfig, EnvironmentConfig
    from agents.services import get_service

    agents = AgentConfig.objects.filter(is_active=True)
    if purpose:
        agent_config = agents.filter(purpose=purpose).first() or agents.first()
    else:
        agent_config = agents.first()
    env_config = (EnvironmentConfig.objects.filter(is_default=True).first()
                  or EnvironmentConfig.objects.first())

    if not agent_config or not env_config:
        return None

    service = get_service()
    session_data = service.create_session(
        agent_id=agent_config.agent_id,
        environment_id=env_config.env_id,
    )
    try:
        service.send_message(session_data['id'], prompt)
        return service.wait_for_response(session_data['id'], timeout=timeout)
    finally:
        # 会话必须回收，否则快速输入这类高频入口会持续堆积云端 session
        try:
            service.cancel_session(session_data['id'])
        except Exception as e:  # 取消失败不影响取回的结果，仅告警
            logger.warning(f'取消云端会话失败: {e}')
