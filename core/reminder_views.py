"""提醒相关视图"""
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from core.models import Reminder
from core.utils import get_visible


@login_required
@require_POST
def reminder_dismiss(request, reminder_id):
    """忽略提醒"""
    reminder = get_visible(Reminder, request.user, id=reminder_id)
    reminder.status = 'dismissed'
    reminder.save(update_fields=['status'])
    return redirect('home')


@login_required
@require_POST
def reminder_done(request, reminder_id):
    """标记提醒为已完成（done，与系统自动触发的 fired 不同，处理完就该移出待处理列表）"""
    reminder = get_visible(Reminder, request.user, id=reminder_id)
    reminder.status = 'done'
    reminder.save(update_fields=['status'])
    return redirect('home')
