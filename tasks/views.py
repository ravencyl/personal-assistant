from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.contrib import messages
from datetime import timedelta
import calendar

from .models import Task
from .forms import TaskForm


@login_required
def task_list(request):
    """任务列表"""
    status_filter = request.GET.get('status', '')
    project_filter = request.GET.get('project', '')

    tasks = Task.objects.filter(user=request.user)

    if status_filter:
        tasks = tasks.filter(status=status_filter)
    if project_filter:
        tasks = tasks.filter(project=project_filter)
    else:
        # 默认不显示已完成和已取消的
        tasks = tasks.exclude(status__in=['done', 'cancelled'])

    # 获取项目列表
    projects = Task.objects.filter(
        user=request.user
    ).exclude(project='').values_list('project', flat=True).distinct()

    return render(request, 'tasks/task_list.html', {
        'tasks': tasks,
        'status_filter': status_filter,
        'project_filter': project_filter,
        'projects': projects,
        'form': TaskForm(),
    })


@login_required
@require_POST
def create_task(request):
    """创建任务"""
    form = TaskForm(request.POST)
    if form.is_valid():
        task = form.save(commit=False)
        task.user = request.user
        task.save()
        messages.success(request, f'任务「{task.title}」已创建')
    else:
        messages.error(request, '创建任务失败，请检查输入')
    return redirect('tasks:task_list')


@login_required
def task_detail(request, task_id):
    """任务详情"""
    task = get_object_or_404(Task, id=task_id, user=request.user)
    subtasks = task.subtasks.all()
    return render(request, 'tasks/task_detail.html', {
        'task': task,
        'subtasks': subtasks,
        'form': TaskForm(instance=task),
    })


@login_required
@require_POST
def update_task(request, task_id):
    """更新任务"""
    task = get_object_or_404(Task, id=task_id, user=request.user)
    form = TaskForm(request.POST, instance=task)
    if form.is_valid():
        form.save()
        messages.success(request, '任务已更新')
    else:
        messages.error(request, '更新失败')
    return redirect('tasks:task_detail', task_id=task.id)


@login_required
@require_POST
def complete_task(request, task_id):
    """完成任务"""
    task = get_object_or_404(Task, id=task_id, user=request.user)
    task.status = 'done'
    task.save(update_fields=['status', 'updated_at'])
    messages.success(request, f'任务「{task.title}」已完成')
    return redirect('tasks:task_list')


@login_required
@require_POST
def delete_task(request, task_id):
    """删除任务"""
    task = get_object_or_404(Task, id=task_id, user=request.user)
    task.delete()
    messages.success(request, '任务已删除')
    return redirect('tasks:task_list')


@login_required
@require_POST
def ai_parse_task(request):
    """自然语言任务解析 - 将自然语言转换为结构化任务"""
    import json
    text = request.POST.get('text', '').strip()
    if not text:
        return JsonResponse({'error': '请输入任务描述'}, status=400)

    try:
        from agents.services import get_service
        from agents.models import AgentConfig, EnvironmentConfig

        service = get_service()

        # 查找 task-agent
        task_agent = AgentConfig.objects.filter(purpose='task', is_active=True).first()
        if not task_agent:
            task_agent = AgentConfig.objects.filter(is_active=True).first()

        if not task_agent:
            return JsonResponse({'error': '请先配置 Agent'}, status=400)

        env_config = EnvironmentConfig.objects.filter(is_default=True).first()
        if not env_config:
            env_config = EnvironmentConfig.objects.first()

        if not env_config:
            return JsonResponse({'error': '请先配置 Environment'}, status=400)

        # 创建 Session 进行解析
        session_data = service.create_session(
            agent_id=task_agent.agent_id,
            environment_id=env_config.env_id,
        )

        parse_prompt = (
            f'请将以下自然语言描述的任务解析为结构化的 JSON 格式。'
            f'输出格式必须是一个 JSON 数组，每个元素包含：'
            f'title(任务标题), description(描述), priority(0-3), due_date(YYYY-MM-DD HH:MM 或 null)。'
            f'如果描述中包含多个任务，请全部解析出来。'
            f'只输出 JSON，不要输出其他内容。\n\n'
            f'任务描述：{text}'
        )

        service.send_message(session_data['id'], parse_prompt)

        # 轮询等待响应
        import time
        max_wait = 60
        start = time.time()
        result_text = ''

        while time.time() - start < max_wait:
            session_info = service.get_session(session_data['id'])
            if session_info.get('status') == 'idle':
                events = service.get_session_events(session_data['id'], limit=50)
                result_text = _extract_text(events)
                break
            time.sleep(1)

        # 尝试解析 JSON
        tasks_data = _parse_tasks_json(result_text)

        # 自动创建任务
        created_tasks = []
        for task_data in tasks_data:
            task = Task.objects.create(
                user=request.user,
                title=task_data.get('title', text[:50]),
                description=task_data.get('description', ''),
                priority=task_data.get('priority', 0),
                ai_generated=True,
            )
            created_tasks.append({
                'id': task.id,
                'title': task.title,
                'priority': task.priority,
            })

        return JsonResponse({
            'success': True,
            'tasks': created_tasks,
        })

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'AI task parse failed: {e}')
        # 降级：直接创建一个简单任务
        task = Task.objects.create(
            user=request.user,
            title=text[:100],
            ai_generated=True,
        )
        return JsonResponse({
            'success': True,
            'tasks': [{'id': task.id, 'title': task.title, 'priority': 0}],
            'fallback': True,
        })


def _extract_text(events):
    """从事件列表中提取文本"""
    messages = []
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                event_type = event.get('type', '')
                if 'assistant' in event_type or event_type == 'agent.message':
                    for c in event.get('content', []):
                        if isinstance(c, dict) and c.get('type') == 'text':
                            messages.append(c.get('text', ''))
    return '\n'.join(messages)


def _parse_tasks_json(text):
    """尝试从 AI 响应中解析 JSON 任务列表"""
    import json
    import re

    # 尝试直接解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass

    # 尝试从文本中提取 JSON 块
    json_match = re.search(r'\[.*\]', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    # 尝试提取代码块中的 JSON
    code_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if code_match:
        try:
            return json.loads(code_match.group(1))
        except json.JSONDecodeError:
            pass

    return []


@login_required
def calendar_view(request):
    """任务日历视图 - 按月展示任务"""
    now = timezone.now()
    year = int(request.GET.get('year', now.year))
    month = int(request.GET.get('month', now.month))

    # 计算当月第一天和最后一天
    first_day = timezone.datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
    last_day = timezone.datetime(
        year, month, calendar.monthrange(year, month)[1], 23, 59, 59,
        tzinfo=timezone.get_current_timezone()
    )

    # 获取当月所有任务
    tasks = Task.objects.filter(
        user=request.user,
        due_date__gte=first_day,
        due_date__lte=last_day,
    ).order_by('due_date')

    # 构建日历数据
    cal = calendar.Calendar(firstweekday=6)  # 周日开始
    month_days = cal.monthdayscalendar(year, month)

    # 按日期分组任务
    tasks_by_day = {}
    for task in tasks:
        if task.due_date:
            day = task.due_date.day
            if day not in tasks_by_day:
                tasks_by_day[day] = []
            tasks_by_day[day].append(task)

    # 导航：上月/下月
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    month_names = ['', '一月', '二月', '三月', '四月', '五月', '六月',
                   '七月', '八月', '九月', '十月', '十一月', '十二月']
    weekday_names = ['日', '一', '二', '三', '四', '五', '六']

    return render(request, 'tasks/calendar.html', {
        'year': year,
        'month': month,
        'month_name': month_names[month],
        'weekday_names': weekday_names,
        'month_days': month_days,
        'tasks_by_day': tasks_by_day,
        'prev_year': prev_year,
        'prev_month': prev_month,
        'next_year': next_year,
        'next_month': next_month,
        'today': now,
    })
