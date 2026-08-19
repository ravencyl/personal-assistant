"""
Qoder Cloud Agents API 服务层
封装所有与 Qoder Cloud Agents 平台的交互
"""

import json
import logging
from typing import Generator

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

    def _get_client(self, timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
            timeout=timeout,
        )

    def _get_stream_client(self, timeout: float = 300.0) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
            timeout=timeout,
        )

    # ==================== Agent 操作 ====================

    def list_agents(self, limit: int = 50) -> list:
        """列出所有 Agent"""
        with self._get_client() as client:
            response = client.get('/agents', params={'limit': limit})
            response.raise_for_status()
            return response.json().get('data', [])

    def get_agent(self, agent_id: str) -> dict:
        """获取单个 Agent 详情"""
        with self._get_client() as client:
            response = client.get(f'/agents/{agent_id}')
            response.raise_for_status()
            return response.json()

    def create_agent(self, name: str, model: str = 'auto',
                     instructions: str = '', system: str = '',
                     tools: list = None, metadata: dict = None) -> dict:
        """创建新 Agent"""
        payload = {
            'name': name,
            'model': model,
        }
        if instructions:
            payload['instructions'] = instructions
        if system:
            payload['system'] = system
        if tools:
            payload['tools'] = tools
        if metadata:
            payload['metadata'] = metadata

        with self._get_client() as client:
            response = client.post('/agents', json=payload)
            response.raise_for_status()
            return response.json()

    def update_agent(self, agent_id: str, version: int, **kwargs) -> dict:
        """更新 Agent（必须携带当前 version）"""
        payload = {'version': version}
        payload.update(kwargs)

        with self._get_client() as client:
            response = client.put(f'/agents/{agent_id}', json=payload)
            response.raise_for_status()
            return response.json()

    def delete_agent(self, agent_id: str) -> None:
        """删除 Agent"""
        with self._get_client() as client:
            response = client.delete(f'/agents/{agent_id}')
            response.raise_for_status()

    # ==================== Environment 操作 ====================

    def list_environments(self) -> list:
        """列出所有 Environment"""
        with self._get_client() as client:
            response = client.get('/environments')
            response.raise_for_status()
            return response.json().get('data', [])

    def create_environment(self, name: str, config: dict = None) -> dict:
        """创建 Environment"""
        payload = {'name': name}
        if config:
            payload['config'] = config
        else:
            payload['config'] = {'type': 'cloud'}

        with self._get_client() as client:
            response = client.post('/environments', json=payload)
            response.raise_for_status()
            return response.json()

    def get_environment(self, env_id: str) -> dict:
        """获取 Environment 详情"""
        with self._get_client() as client:
            response = client.get(f'/environments/{env_id}')
            response.raise_for_status()
            return response.json()

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

        with self._get_client() as client:
            response = client.post('/sessions', json=payload)
            response.raise_for_status()
            return response.json()

    def get_session(self, session_id: str) -> dict:
        """获取 Session 详情"""
        with self._get_client() as client:
            response = client.get(f'/sessions/{session_id}')
            response.raise_for_status()
            return response.json()

    def list_sessions(self, limit: int = 50) -> list:
        """列出所有 Session"""
        with self._get_client() as client:
            response = client.get('/sessions', params={'limit': limit})
            response.raise_for_status()
            return response.json().get('data', [])

    def send_message(self, session_id: str, text: str) -> dict:
        """向 Session 发送消息"""
        payload = {
            'events': [{
                'type': 'user.message',
                'content': [{'type': 'text', 'text': text}]
            }]
        }

        with self._get_client() as client:
            response = client.post(
                f'/sessions/{session_id}/events',
                json=payload
            )
            response.raise_for_status()
            return response.json()

    def cancel_session(self, session_id: str) -> dict:
        """取消正在执行的 Session"""
        with self._get_client() as client:
            response = client.post(f'/sessions/{session_id}/cancel')
            response.raise_for_status()
            return response.json()

    def get_session_events(self, session_id: str, limit: int = 100) -> list:
        """获取 Session 的事件历史"""
        with self._get_client() as client:
            response = client.get(
                f'/sessions/{session_id}/events',
                params={'limit': limit}
            )
            response.raise_for_status()
            return response.json().get('data', [])

    def stream_events(self, session_id: str) -> Generator:
        """
        SSE 事件流监听
        生成器，逐条返回 Session 事件
        """
        import sseclient

        with self._get_stream_client(timeout=300.0) as client:
            with client.stream(
                'GET',
                f'/sessions/{session_id}/events',
                params={'stream': 'true'},
                headers={**self.headers, 'Accept': 'text/event-stream'},
            ) as response:
                response.raise_for_status()
                sse = sseclient.SSEClient(response)
                for event in sse.events():
                    try:
                        data = json.loads(event.data)
                        yield data
                    except json.JSONDecodeError:
                        logger.warning(f'Failed to parse SSE event: {event.data}')
                        continue

    # ==================== 工具方法 ====================

    def verify_connection(self) -> bool:
        """验证 API 连接是否正常"""
        try:
            self.list_agents(limit=1)
            return True
        except Exception as e:
            logger.error(f'Connection verification failed: {e}')
            return False

    def get_default_environment_id(self) -> str:
        """获取默认 Environment ID"""
        configured = settings.QODER_DEFAULT_ENVIRONMENT_ID
        if configured:
            return configured
        # 如果没有配置，获取第一个可用的
        envs = self.list_environments()
        if envs:
            return envs[0]['id']
        return ''


def get_service() -> QoderAgentService:
    """获取默认的 QoderAgentService 实例"""
    return QoderAgentService()
