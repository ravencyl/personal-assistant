import logging

from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_GET, require_POST

from .services import get_service
from .models import AgentConfig, EnvironmentConfig

logger = logging.getLogger(__name__)


@require_GET
@staff_member_required
def api_status(request):
    """检查 Qoder Cloud Agents API 连接状态"""
    service = get_service()
    is_connected = service.verify_connection()
    return JsonResponse({
        'connected': is_connected,
        'base_url': service.base_url,
    })


@require_POST
@staff_member_required
def sync_agents(request):
    """从 Qoder 平台同步 Agent 列表到本地"""
    service = get_service()
    try:
        remote_agents = service.list_agents()
        synced = 0
        for agent_data in remote_agents:
            agent_id = agent_data['id']
            defaults = {
                'name': agent_data.get('name', ''),
                'model': agent_data.get('model', 'auto'),
                'instructions': agent_data.get('instructions', ''),
                'system_prompt': agent_data.get('system', ''),
                'tools': agent_data.get('tools', []),
                'metadata': agent_data.get('metadata', {}),
                'version': agent_data.get('version', 1),
            }
            AgentConfig.objects.update_or_create(
                agent_id=agent_id,
                defaults=defaults
            )
            synced += 1

        # 同步 Environments
        remote_envs = service.list_environments()
        for env_data in remote_envs:
            EnvironmentConfig.objects.update_or_create(
                env_id=env_data['id'],
                defaults={
                    'name': env_data.get('name', ''),
                    'description': env_data.get('description', ''),
                    'config': env_data.get('config', {}),
                }
            )

        return JsonResponse({
            'success': True,
            'agents_synced': synced,
            'environments_synced': len(remote_envs),
        })
    except Exception as e:
        logger.error(f'Failed to sync agents: {e}')
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
