"""
Qoder Cloud Agents API 服务层
封装所有与 Qoder Cloud Agents 平台的交互
"""

import logging
import time

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class QoderAgentService:
    """Qoder Cloud Agents API 封装"""

    def __init__(self, access_token: str = None, base_url: str = None):
        self.access_token = access_token or settings.QODER_ACCESS_TOKEN
        self.base_url = (base_url or settings.QODER_API_BASE_URL).rstrip('/')
        self.headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
        }
        # 复用同一个 httpx.Client，避免每次请求都新建 TCP 连接（轮询场景尤其重要）
        self._client = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=self.headers,
                timeout=30.0,
            )
        return self._client

    # ==================== Agent 操作 ====================

    def list_agents(self, limit: int = 50) -> list:
        """列出所有 Agent"""
        response = self._get_client().get('/agents', params={'limit': limit})
        response.raise_for_status()
        return response.json().get('data', [])

    def create_agent(self, name: str, model: str = 'auto',
                     instructions: str = '',
                     tools: list = None, metadata: dict = None) -> dict:
        """创建新 Agent（提示词只走 instructions，平台无独立的 system 字段）"""
        payload = {
            'name': name,
            'model': model,
        }
        if instructions:
            payload['instructions'] = instructions
        if tools:
            payload['tools'] = tools
        if metadata:
            payload['metadata'] = metadata

        response = self._get_client().post('/agents', json=payload)
        response.raise_for_status()
        return response.json()

    # ==================== Environment 操作 ====================

    def list_environments(self) -> list:
        """列出所有 Environment"""
        response = self._get_client().get('/environments')
        response.raise_for_status()
        return response.json().get('data', [])

    # ==================== Session 操作 ====================

    def create_session(self, agent_id: str, environment_id: str,
                       agent_version: int = None) -> dict:
        """创建 Session"""
        if agent_version:
            agent = {'id': agent_id, 'version': agent_version}
        else:
            agent = agent_id

        payload = {
            'agent': agent,
            'environment_id': environment_id,
        }

        response = self._get_client().post('/sessions', json=payload)
        response.raise_for_status()
        return response.json()

    # 以下方法（get_session / send_message / cancel_session）无外部调用者，
    # 但都是 session 生命周期管理的基础原语，不属于可删的死代码。
    def get_session(self, session_id: str) -> dict:
        """获取 Session 详情"""
        response = self._get_client().get(f'/sessions/{session_id}')
        response.raise_for_status()
        return response.json()

    def send_message(self, session_id: str, text: str) -> dict:
        """向 Session 发送消息"""
        payload = {
            'events': [{
                'type': 'user.message',
                'content': [{'type': 'text', 'text': text}]
            }]
        }

        response = self._get_client().post(
            f'/sessions/{session_id}/events',
            json=payload
        )
        response.raise_for_status()
        return response.json()

    def cancel_session(self, session_id: str) -> dict:
        """取消正在执行的 Session"""
        response = self._get_client().post(f'/sessions/{session_id}/cancel')
        response.raise_for_status()
        return response.json()

    def get_recent_events(self, session_id: str, limit: int = 100,
                          max_walks: int = 50) -> list:
        """取「最新在前」的事件窗口（平台支持 order=desc，2026-09-22 实测）

        聊天轮询只需要最近一轮：本轮回复一定比本轮用户消息新，从最新往旧
        找到第一条 user.message 即可收口。正常情况一页（100 条，平台上限）
        必含完整本轮，**与会话总长度无关**。

        事件历史接口只能从最旧往新翻（第一页永远从会话创建事件开始），
        那条路曾在老会话上出过线上故障：事件总数超过 100 后窗口停在历史里，
        最近几轮的回复全落在窗外，对话 42 连续多轮「AI 这轮没有返回内容」
        （模型其实每轮都在平台上回了）。所以这里永远走 desc：新事件在前，
        老会话多长都不会把本轮裁掉。

        病态情况（窗口里居然没有 user.message，说明一轮产生的事件比一页还
        多）才继续按游标向更旧翻，直到找到本轮的用户消息为止；max_walks
        只是防平台游标异常死循环的保险丝，正常永远用不到。
        """
        events: list = []
        page = None
        for _ in range(max_walks):
            params = {'limit': limit, 'order': 'desc'}
            if page:
                params['page'] = page
            response = self._get_client().get(
                f'/sessions/{session_id}/events',
                params=params
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get('data', [])
            events.extend(data)
            if any(isinstance(e, dict) and e.get('type') == 'user.message'
                   for e in data):
                break
            if not payload.get('has_more'):
                break
            next_page = payload.get('next_page')
            if not next_page or next_page == page:
                break  # 游标不前进 = 平台异常，宁可返回窗口内事件也不死循环
            page = next_page
        return events

    # ==================== 高级方法 ====================

    def wait_for_response(self, session_id: str, timeout: float = 120.0,
                          poll_interval: float = 1.0) -> str:
        """发送消息后轮询等待 AI 响应，返回 assistant 文本（超时或无响应返回空串）

        采用「状态 + 事件」双重确认：status 变为 idle 后还需提取到本轮回复才算完成，
        避免平台状态切换延迟导致首次轮询误判为已完成。
        """
        start = time.time()
        idle_hits = 0
        while time.time() - start < timeout:
            info = self.get_session(session_id)
            if info.get('status') == 'idle':
                events = self.get_recent_events(session_id)
                text = self.extract_latest_reply(events)
                if text:
                    return text
                # idle 但尚未提取到回复：状态可能还未同步，连续多次仍无则放弃
                idle_hits += 1
                if idle_hits >= 3:
                    return ''
            else:
                idle_hits = 0
            time.sleep(poll_interval)
        return ''

    def poll_turn(self, session_id: str) -> dict:
        """单次、**不 sleep** 地查本轮 AI 是否回完（聊天页面用）

        返回 {'state': 'processing' | 'ready' | 'empty', 'text': str}：
        - processing：session 还在跑
        - ready：拿到本轮 assistant 文本
        - empty：session 已 idle 但提不到本轮文本（真的没回复，或被取消）

        与 wait_for_response 的区别：那个给服务端内部的一轮性任务用（报告生成、
        快速输入解析等，本来就在循环里，等完才返回），这个给“不能占住请求 worker”
        的对话路径用，循环由浏览器带着节奏发。空回复判定不在此处下结论：
        平台 status 比事件写入快，得由调用方给宽限期（见 chat.models.TURN_IDLE_GRACE_SECONDS）。
        """
        info = self.get_session(session_id)
        if info.get('status') != 'idle':
            return {'state': 'processing', 'text': ''}
        events = self.get_recent_events(session_id)
        text = self.extract_latest_reply(events)
        return {'state': 'ready' if text else 'empty', 'text': text}

    @staticmethod
    def extract_latest_reply(events_desc: list) -> str:
        """从「最新在前」的事件窗口提取本轮回复（extract_assistant_text 的 desc 版）

        从最新事件往旧收集 assistant 文本，遇到最近一条 user.message 就停：
        它们之间的就是本轮回复。窗口里没有 user.message 时返回空串——
        宁可把这轮判成无回复，也不能把更早轮次的回复顶给用户。
        """
        if not isinstance(events_desc, list):
            return ''

        texts = []
        seen_user_message = False
        for event in events_desc:
            if not isinstance(event, dict):
                continue
            event_type = event.get('type', '')
            if event_type == 'user.message':
                seen_user_message = True
                break
            if 'assistant' in event_type or event_type == 'agent.message':
                for c in event.get('content', []):
                    if isinstance(c, dict) and c.get('type') == 'text':
                        texts.append(c.get('text', ''))
        if not seen_user_message:
            return ''
        # 收集顺序是新→旧，反转回阅读顺序
        return '\n'.join(reversed(texts))

    def verify_connection(self) -> bool:
        """验证 API 连接是否正常"""
        try:
            self.list_agents(limit=1)
            return True
        except Exception as e:
            logger.error(f'Connection verification failed: {e}')
            return False


def get_service() -> QoderAgentService:
    """获取默认的 QoderAgentService 实例"""
    return QoderAgentService()
