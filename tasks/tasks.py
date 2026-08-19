from celery import shared_task
from django.utils import timezone
from datetime import timedelta


@shared_task
def check_task_reminders():
    """检查即将到期的任务并发送提醒"""
    from .models import Task
    from django.contrib.auth import get_user_model

    User = get_user_model()

    # 查找 15 分钟内即将到期且未发送提醒的任务
    now = timezone.now()
    soon = now + timedelta(minutes=15)

    tasks = Task.objects.filter(
        due_date__lte=soon,
        due_date__gte=now,
        reminder_sent=False,
        status__in=['pending', 'in_progress'],
    ).select_related('user')

    for task in tasks:
        # 这里可以扩展为发送邮件/通知
        task.reminder_sent = True
        task.save(update_fields=['reminder_sent'])

    return f'Checked {tasks.count()} task reminders'


@shared_task
def cleanup_old_tasks(days=30):
    """清理已完成超过指定天数的任务"""
    from .models import Task
    from django.utils import timezone
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Task.objects.filter(
        status='done',
        updated_at__lt=cutoff,
    ).delete()

    return f'Deleted {deleted} old tasks'
