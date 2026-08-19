"""
初始化预定义的 Agent 配置到 Qoder Cloud Agents 平台
"""
import logging
from django.core.management.base import BaseCommand
from agents.services import QoderAgentService
from agents.models import AgentConfig

logger = logging.getLogger(__name__)

# PRD 第 4 节定义的预定义 Agent 配置
PREDEFINED_AGENTS = [
    {
        'name': 'general-assistant',
        'model': 'auto',
        'instructions': '你是一个通用 AI 助手，可以帮助用户回答各种问题。你善于搜索网络获取最新信息，并能抓取网页内容进行详细分析。请用中文回答。',
        'tools': [{
            'type': 'agent_toolset_20260401',
            'enabled_tools': ['WebSearch', 'WebFetch']
        }],
        'metadata': {'purpose': 'general'},
        'purpose': 'general',
    },
    {
        'name': 'knowledge-agent',
        'model': 'qmodel_latest',  # Qwen3.7-Max
        'instructions': '你是一个知识库问答助手。基于提供的知识库内容回答用户问题。如果知识库中没有相关信息，请明确告知用户。请用中文回答。',
        'tools': [{
            'type': 'agent_toolset_20260401',
            'enabled_tools': ['Read', 'WebSearch', 'WebFetch']
        }],
        'metadata': {'purpose': 'knowledge'},
        'purpose': 'knowledge',
    },
    {
        'name': 'task-agent',
        'model': 'auto',
        'instructions': '你是一个任务管理助手。你可以帮助用户创建、管理和分解任务。当用户描述一个复杂目标时，将其分解为可执行的子任务。请用中文回答，并以结构化格式输出任务列表。',
        'tools': [{
            'type': 'agent_toolset_20260401',
            'enabled_tools': ['Bash', 'Write', 'Read']
        }],
        'metadata': {'purpose': 'task'},
        'purpose': 'task',
    },
    {
        'name': 'content-agent',
        'model': 'qmodel',  # Qwen3.7-Plus
        'instructions': '你是一个内容处理助手。你可以帮助用户总结网页内容、生成文章摘要、推荐相关内容。请用中文输出简洁准确的摘要。',
        'tools': [{
            'type': 'agent_toolset_20260401',
            'enabled_tools': ['WebFetch', 'WebSearch', 'Write']
        }],
        'metadata': {'purpose': 'content'},
        'purpose': 'content',
    },
    {
        'name': 'code-agent',
        'model': 'dmodel',  # DeepSeek-V4-Pro
        'instructions': '你是一个编程助手。你可以帮助用户编写代码、审查代码、调试问题。支持多种编程语言。请用中文解释代码逻辑。',
        'tools': [{
            'type': 'agent_toolset_20260401',
            'enabled_tools': ['Bash', 'Read', 'Write', 'Edit', 'Grep', 'Glob']
        }],
        'metadata': {'purpose': 'code'},
        'purpose': 'code',
    },
]


class Command(BaseCommand):
    help = '在 Qoder Cloud Agents 平台创建预定义的 Agent 配置，并同步到本地数据库'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='仅显示将要创建的配置，不实际执行',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        service = QoderAgentService()

        # 验证连接
        self.stdout.write('正在验证 API 连接...')
        if not service.verify_connection():
            self.stderr.write(self.style.ERROR(
                'API 连接失败！请检查 .env 中的 QODER_ACCESS_TOKEN 是否正确。'
            ))
            return

        self.stdout.write(self.style.SUCCESS('API 连接正常'))

        # 获取已有的远程 Agent
        existing_agents = {a['name']: a for a in service.list_agents()}

        created_count = 0
        for agent_def in PREDEFINED_AGENTS:
            name = agent_def['name']

            if dry_run:
                if name in existing_agents:
                    self.stdout.write(f'  [已存在] {name}')
                else:
                    self.stdout.write(f'  [将创建] {name}')
                continue

            if name in existing_agents:
                self.stdout.write(f'  [跳过] {name} 已存在')
                # 同步到本地
                remote = existing_agents[name]
                AgentConfig.objects.update_or_create(
                    agent_id=remote['id'],
                    defaults={
                        'name': remote.get('name', name),
                        'model': remote.get('model', 'auto'),
                        'instructions': remote.get('instructions', ''),
                        'system_prompt': remote.get('system', ''),
                        'tools': remote.get('tools', []),
                        'metadata': remote.get('metadata', {}),
                        'version': remote.get('version', 1),
                        'purpose': agent_def['purpose'],
                    }
                )
                continue

            # 创建 Agent
            self.stdout.write(f'  [创建中] {name}...')
            try:
                result = service.create_agent(
                    name=agent_def['name'],
                    model=agent_def['model'],
                    instructions=agent_def['instructions'],
                    tools=agent_def['tools'],
                    metadata=agent_def['metadata'],
                )
                agent_id = result['id']

                # 同步到本地
                AgentConfig.objects.update_or_create(
                    agent_id=agent_id,
                    defaults={
                        'name': result['name'],
                        'model': result.get('model', 'auto'),
                        'instructions': result.get('instructions', ''),
                        'system_prompt': result.get('system', ''),
                        'tools': result.get('tools', []),
                        'metadata': result.get('metadata', {}),
                        'version': result.get('version', 1),
                        'purpose': agent_def['purpose'],
                    }
                )
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f'    已创建: {agent_id}'))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'    创建失败: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'\n完成！共创建 {created_count} 个 Agent。'
        ))
