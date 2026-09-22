"""活动详情页及其子资源端点（子任务 / 评论 / 状态 / 附件）"""
import json
import re
from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.tags import apply_tags, tag_names, tag_suggestions, used_tags
from core.upload import MAX_UPLOAD_SIZE, MAX_UPLOAD_SIZE_MB
from core.utils import get_visible, visible_qs, wants_json

from ..models import Activity, Participant, Attachment, ActivityComment
from ..services import (InputError, add_expense, change_activity_status,
                        clean_amount, create_activity_from_parsed,
                        start_due_activities)
from ..utils import log_activity, normalize_input, resolve_participants
from ._common import _participant_skip_text, _user_tag_names, attach_costs


def _subactivity_timeline(activity):
    """子任务时间轴数据：按可用日期（开始优先，其次结束）从晚到早倒序，无日期排最后

    倒序是用户要求（2026-09-22）：最近的排最上面。详情页首次渲染与内联手动
    创建端点的局部刷新共用。
    """
    children = list(activity.children.prefetch_related('tags', 'participants').all())
    # 倒序用负序号实现，仍与「无日期排最后」同一个稳定排序，不引入两次 sort
    children.sort(key=lambda c: ((c.start_date or c.end_date) is None,
                                 -(c.start_date or c.end_date or date.min).toordinal()))
    for child in children:
        d = child.start_date or child.end_date
        child.timeline_label = d.strftime('%m-%d') if d else '未设定'
        child.timeline_year = d.strftime('%Y') if d else ''
        # 子活动的费用合计（自身 Expense + 后代 Expense，用于时间轴展示）
        child.expenses_total = child.total_cost
    return children


@login_required
def activity_detail(request, activity_id):
    """活动详情（含子任务时间轴、费用明细与操作日志）"""
    # 到期活动自动转进行中（与 cron 同一份判定，见 services.start_due_activities）
    start_due_activities(request.user)
    activity = get_visible(Activity, request.user, id=activity_id)

    children = _subactivity_timeline(activity)

    # 费用明细（tags_str 供模板徽章展示与编辑回填一次取齐，避免模板里 join 不动 M2M）
    expenses = list(activity.expenses.all())
    for e in expenses:
        e.tags_list = list(tag_names(e))
        e.tags_str = ', '.join(e.tags_list)

    # 常用费用标签 chips：当前用户用过的 expense 标签，按使用频次排序，
    # 上限 8 个（口径与 used_tags 一致；点击即追加到表单标签输入框，纯前端）
    expense_tag_chips = list(
        used_tags('expense', request.user).annotate(n=Count('expense'))
        .order_by('-n', 'name').values_list('name', flat=True)[:8])

    # 附件
    attachments = list(activity.attachments.all())

    # 子任务完成度（详情页统计卡进度条）
    subtask_done_count = sum(1 for c in children if c.status == 'done')

    # 跨模块关联推荐
    from core.cross_link import get_related_content
    related = get_related_content(request.user, Activity, activity, limit=5)

    # 评论时间线（追加式，正序；可见性跟随活动，无需再过滤）
    comments = activity.comments.select_related('user')

    # 前置依赖（详情页右列展示 + 手动添加/移除入口）
    blocked_deps = list(activity.blocked_by.all())

    # 「问 AI」深链：跳聊天页并预填针对当前活动的提问（chat 页 ?ask= 处理）。
    # 只填不发（与 chips 同一约定），用户可改两个字再发
    chat_ask_url = (
        reverse('chat:conversation_list') + '?' +
        urlencode({'ask': f'帮我看看「{activity.name}」这个活动，'
                          f'有什么要注意或建议的吗？',
                   'pin': activity.id})
    )

    return render(request, 'activities/activity_detail.html', {
        'activity': activity,
        'max_upload_mb': MAX_UPLOAD_SIZE_MB,
        'children': children,
        'participants': activity.participants.all(),
        'status_choices': Activity.STATUS_CHOICES,
        'logs': activity.logs.select_related('user')[:50],
        'expenses': expenses,
        # 常用费用标签 chips（scope=expense，频次序，上限 8）
        'expense_tag_chips': expense_tag_chips,
        'comments': comments,
        'today_date': timezone.localdate().isoformat(),
        'attachments': attachments,
        'subtask_done_count': subtask_done_count,
        'blocked_deps': blocked_deps,
        'related_articles': related.get('articles', []),
        'related_notes': related.get('notes', []),
        # 手动内联创建子任务表单的 autocomplete 建议（scope=activity）
        'tag_suggestions': _user_tag_names(request.user),
        # 费用表单的 autocomplete 建议（scope=expense，与活动标签隔离）
        'expense_tag_suggestions': tag_suggestions('expense', request.user),
        'participant_suggestions': list(Participant.objects.filter(
            user=activity.user).values_list('name', flat=True).order_by('name')),
        'chat_ask_url': chat_ask_url,
    })


@login_required
@require_POST
def activity_set_status(request, activity_id):
    """快捷修改活动状态"""
    activity = get_visible(Activity, request.user, id=activity_id)
    status = request.POST.get('status', '')
    is_fragment = bool(request.headers.get('HX-Request'))
    if status not in dict(Activity.STATUS_CHOICES):
        if is_fragment:
            return HttpResponse('错误：无效的状态值', status=400)
        messages.error(request, '无效的状态值')
    else:
        if status != activity.status:
            # 落库 + 日志走 services 统一实现（与 AI 单改 / AI 批量同一口径）
            _old_label, new_label = change_activity_status(activity, request.user, status)
            if not is_fragment:
                messages.success(request, f'状态已更新为「{new_label}」')
        if is_fragment:
            attach_costs([activity])
            # 局部刷新重渲染整张卡：按日期重新推导徽标，避免丢失「今日开始/今日结束」标签
            today = timezone.localdate()
            badge = 'today' if activity.start_date == today else (
                'ending' if activity.end_date == today else '')
            return render(request, 'activities/_daily_card.html',
                          {'activity': activity, 'badge': badge})
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def add_subactivity(request, activity_id):
    """快捷创建子活动（仅填名称；归属与父活动一致）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, '子活动名称不能为空')
    else:
        # 与其余创建路径走同一个 services（仅多一个「今天结束」的默认值），
        # 子任务创建与双条日志（sub_created + created）由 services 统一发出
        result = create_activity_from_parsed(
            request.user, {'name': name, 'end_date': timezone.localdate()},
            parent=activity)
        messages.success(request, f"子活动「{result['activity'].name}」已创建")
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def activity_comment_add(request, activity_id):
    """追加评论（详情页内联表单；AI 经 activities.get/comments 工具读取）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    content = (request.POST.get('content') or '').strip()
    if not content:
        messages.error(request, '评论内容不能为空')
    else:
        ActivityComment.objects.create(activity=activity, user=request.user, content=content)
        log_activity(request.user, activity, 'commented', content[:100])
        messages.success(request, '评论已添加')
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def activity_comment_delete(request, comment_id):
    """删除评论：仅评论人或超级用户（活动可见性已由 get_visible 门禁）"""
    comment = get_object_or_404(ActivityComment, id=comment_id)
    activity = get_visible(Activity, request.user, id=comment.activity_id)
    if comment.user_id != request.user.id and not request.user.is_superuser:
        messages.error(request, '只能删除自己的评论')
        return redirect('activities:activity_detail', activity.id)
    comment.delete()
    messages.success(request, '评论已删除')
    return redirect('activities:activity_detail', activity.id)


@login_required
@require_POST
def activity_quick_sub(request, activity_id):
    """详情页快速创建子任务（解析后预览确认；归属当前活动）"""
    activity = get_visible(Activity, request.user, id=activity_id)
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    data = normalize_input(data, timezone.localdate())
    if not data.get('name'):
        return JsonResponse({'error': '子任务名称不能为空'}, status=400)

    # 与手动内联表单同口径：花费记在子任务自己名下（时间轴按子任务展示费用），
    # 而不是记到父活动上；一句创建属自动识别，不新建参与者。
    result = create_activity_from_parsed(request.user, data, parent=activity,
                                        source='一句话创建')
    child = result['activity']

    return JsonResponse({
        'id': child.id,
        'name': child.name,
        'url': reverse('activities:activity_detail', args=[child.id]),
        'note': _participant_skip_text(result['skipped']),
    })


def _split_name_input(value):
    """内联表单的标签/参与者输入 → 去重列表（兼容逗号/顿号分隔与 # @ 前缀）"""
    if isinstance(value, str):
        items = re.split(r'[,，、]', value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    names, seen = [], set()
    for item in items:
        name = str(item).strip().lstrip('#@').strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name[:100])
    return names[:10]


def _parse_date_input(value):
    """内联表单日期输入 → date；空值返回 None，非法值抛 ValueError"""
    text = str(value or '').strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


@login_required
@require_POST
def subactivity_manual_create(request, activity_id):
    """详情页内联手动表单创建子任务（JSON，支持日期/状态/费用/标签/参与者）

    与 AI 快速入口 activity_quick_sub 的区别：字段由用户显式填写，校验失败返回
    400 + 友好文案（不静默丢弃），费用记在子任务自己名下便于时间轴直接展示。
    """
    activity = get_visible(Activity, request.user, id=activity_id)
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': '请求数据格式错误'}, status=400)

    name = str(data.get('name') or '').strip()
    if not name:
        return JsonResponse({'error': '子任务名称不能为空'}, status=400)

    try:
        start_date = _parse_date_input(data.get('start_date'))
        end_date = _parse_date_input(data.get('end_date'))
    except ValueError:
        return JsonResponse({'error': '日期格式不正确，请重新选择'}, status=400)
    if start_date and end_date and end_date < start_date:
        return JsonResponse({'error': '结束日期不能早于开始日期'}, status=400)

    status = str(data.get('status') or 'planned')
    if status not in dict(Activity.STATUS_CHOICES):
        status = 'planned'

    amount = None
    try:
        # 用字符串构造 Decimal，避免 float 二进制的 0000000001 尾差写进库
        amount = clean_amount(data.get('amount'), label='费用金额')
    except InputError as e:
        return JsonResponse({'error': str(e)}, status=400)

    child = Activity.objects.create(
        user=activity.user,          # 归属继承父活动
        name=name[:255],
        parent=activity,
        start_date=start_date,
        end_date=end_date,
        status=status,
    )
    if amount:
        # 0 与空值同样视为「本次没花钱」，不建 0 元记录
        add_expense(child, activity.user, amount, note=f'子任务「{child.name}」费用')
    tags = _split_name_input(data.get('tags'))
    if tags:
        apply_tags(child, tags)
    participant_names = _split_name_input(data.get('participants'))
    created = []
    if participant_names:
        # 内联表单为用户显式填写：先按大小写不敏感复用已有写法，确实没有才新建
        participants, _skipped, created = resolve_participants(
            activity.user, participant_names, create_missing=True)
        child.participants.set(participants)

    log_activity(request.user, activity, 'sub_created', f'创建子任务「{child.name}」')
    log_activity(request.user, child, 'created', f'在父活动「{activity.name}」下创建')

    # 返回重渲染后的子任务列表片段，供前端局部刷新（避开整页重载闪烁）
    children = _subactivity_timeline(activity)
    return JsonResponse({
        'id': child.id,
        'name': child.name,
        'url': reverse('activities:activity_detail', args=[child.id]),
        'note': f"已新建参与者「{'、'.join(created)}」" if created else '',
        'children_count': len(children),
        'children_html': render_to_string('activities/_subactivity_items.html', {
            'activity': activity,
            'children': children,
        }, request=request),
    })


@login_required
def blocked_search(request, activity_id):
    """搜索可设为前置依赖的活动（JSON；排除自己与已添加的）"""
    activity = visible_qs(Activity, request.user).filter(id=activity_id).first()
    if not activity:
        # JSON 端点：越权/找不到统一 JSON 404，不能抛 Http404 返回 HTML 错误页
        return JsonResponse({'error': '活动不存在或无权访问'}, status=404)
    q = (request.GET.get('q') or '').strip()
    existing = set(activity.blocked_by.values_list('id', flat=True)) | {activity.id}
    qs = visible_qs(Activity, request.user).exclude(id__in=existing)
    if q:
        qs = qs.filter(name__icontains=q)
    return JsonResponse({'items': [{
        'id': a.id,
        'name': a.name,
        'status_label': a.get_status_display(),
    } for a in qs.order_by('-updated_at')[:8]]})


@login_required
@require_POST
def blocked_add(request, activity_id):
    """添加前置依赖（JSON；环检测与 AI 工具同一口径）"""
    activity = visible_qs(Activity, request.user).filter(id=activity_id).first()
    if not activity:
        return JsonResponse({'error': '活动不存在或无权访问'}, status=404)
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    dep = visible_qs(Activity, request.user).filter(id=data.get('activity_id')).first()
    if not dep:
        return JsonResponse({'error': '前置活动不存在或无权访问'}, status=404)
    if dep.id == activity.id:
        return JsonResponse({'error': '不能依赖自己'}, status=400)
    if activity.blocked_by.filter(id=dep.id).exists():
        return JsonResponse({'error': '已添加过该前置依赖'}, status=400)
    if activity.would_create_cycle([dep.id]):
        return JsonResponse({'error': '不能形成循环依赖（A→B→C→A 不允许）'}, status=400)
    activity.blocked_by.add(dep)
    log_activity(request.user, activity, 'edited', f'添加前置依赖「{dep.name}」')
    return JsonResponse({
        'ok': True,
        'id': dep.id,
        'name': dep.name,
        'status_label': dep.get_status_display(),
        'done': dep.status == 'done',
    })


@login_required
@require_POST
def blocked_remove(request, activity_id, dep_id):
    """移除前置依赖（JSON）"""
    activity = visible_qs(Activity, request.user).filter(id=activity_id).first()
    if not activity:
        return JsonResponse({'error': '活动不存在或无权访问'}, status=404)
    # 前置只会从本人可见的候选里添加进来，直接从 M2M 里找即可；找不到一并 404
    dep = activity.blocked_by.filter(id=dep_id).first()
    if not dep:
        return JsonResponse({'error': '前置依赖不存在'}, status=404)
    activity.blocked_by.remove(dep)
    log_activity(request.user, activity, 'edited', f'移除前置依赖「{dep.name}」')
    return JsonResponse({'ok': True})


@login_required
@require_POST
def attachment_upload(request, activity_id):
    """上传附件"""
    activity = get_visible(Activity, request.user, id=activity_id)
    # 详情页附件表单是整页 POST（无 hx-*），非 AJAX 时必须回跳，否则浏览器直接显示 JSON
    is_ajax = wants_json(request)

    def _fail(message):
        if is_ajax:
            return JsonResponse({'error': message}, status=400)
        messages.error(request, message)
        return redirect('activities:activity_detail', activity.id)

    uploaded_file = request.FILES.get('file')
    if not uploaded_file:
        return _fail('请选择文件')

    if uploaded_file.size > MAX_UPLOAD_SIZE:
        return _fail(f'文件大小不能超过 {MAX_UPLOAD_SIZE_MB}MB')

    attachment = Attachment.objects.create(
        activity=activity,
        user=request.user,
        file=uploaded_file,
        filename=uploaded_file.name,
        content_type=uploaded_file.content_type or '',
        size=uploaded_file.size,
    )
    log_activity(request.user, activity, 'edited', f'上传附件「{attachment.filename}」')

    if not is_ajax:
        messages.success(request, f'附件「{attachment.filename}」已上传')
        return redirect('activities:activity_detail', activity.id)

    return JsonResponse({
        'id': attachment.id,
        'filename': attachment.filename,
        'size': attachment.size_display,
        'is_image': attachment.is_image,
        'url': attachment.file.url,
    })


@login_required
@require_POST
def attachment_delete(request, attachment_id):
    """删除附件"""
    attachment = get_visible(Attachment, request.user, id=attachment_id)
    activity = attachment.activity
    filename = attachment.filename
    attachment.file.delete()  # 删除物理文件
    attachment.delete()
    log_activity(request.user, activity, 'edited', f'删除附件「{filename}」')

    if wants_json(request):
        return JsonResponse({'ok': True})

    return redirect('activities:activity_detail', activity.id)
