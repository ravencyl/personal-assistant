"""建议交互端点：关闭 / 已读 / 工具直操（JSON，由前端 fetch 消费）"""
import hmac
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from core.agent_registry import ToolError, get_tool, make_action_token
from core.models import SuggestionState
from core.suggestions import SUGGESTION_TOOLS, invalidate_user_caches

logger = logging.getLogger(__name__)


@login_required
@require_POST
def suggestion_dismiss(request):
    """关闭建议：写 SuggestionState（幂等），失效建议缓存"""
    try:
        body = json.loads(request.body)
        key = body.get('key', '').strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': '无效的请求体'}, status=400)

    if not key or len(key) > 128:
        return JsonResponse({'error': '无效的建议指纹'}, status=400)

    SuggestionState.objects.update_or_create(
        user=request.user,
        fingerprint=key,
        defaults={'action': 'dismissed'},
    )
    invalidate_user_caches(request.user.id)
    logger.info('关闭建议 user=%s key=%s', request.user.id, key)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def suggestion_read(request):
    """标记已读：仅在该指纹无记录时创建，不覆盖 dismissed"""
    try:
        body = json.loads(request.body)
        key = body.get('key', '').strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': '无效的请求体'}, status=400)

    if not key or len(key) > 128:
        return JsonResponse({'error': '无效的建议指纹'}, status=400)

    SuggestionState.objects.get_or_create(
        user=request.user,
        fingerprint=key,
        defaults={'action': 'read'},
    )
    return JsonResponse({'ok': True})


def _valid_params(params):
    """params 只允许标量；batch_status 额外允许纯整数 id 列表"""
    if not isinstance(params, dict):
        return False
    for value in params.values():
        if isinstance(value, bool) or isinstance(value, (int, float, str)):
            continue
        if (isinstance(value, list)
                and value
                and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
            continue
        return False
    return True


@login_required
@require_POST
def suggestion_tool_run(request):
    """执行建议附带的 Agent 工具（白名单 + 高危两步确认）

    body: {'key': 建议指纹, 'tool', 'params', 'confirm_token': 可选}
    - 需确认的工具首次返回 need_confirm + token，二次携 token 才执行
    - activities.batch_status 携 target_ids 时直达 apply 函数（跳过关键词预览）
    - 执行成功自动标记建议已读并失效缓存
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'error': '无效的请求体'}, status=400)

    key = str(body.get('key') or '').strip()[:128]
    tool_name = str(body.get('tool') or '').strip()
    params = body.get('params') or {}
    confirm_token = str(body.get('confirm_token') or '')

    # 白名单与参数校验：不暴露任意工具调用
    if tool_name not in SUGGESTION_TOOLS:
        return JsonResponse({'ok': False, 'error': '该操作不在允许范围'}, status=400)
    if not _valid_params(params):
        return JsonResponse({'ok': False, 'error': '无效的操作参数'}, status=400)
    tool = get_tool(tool_name)
    if not tool:
        return JsonResponse({'ok': False, 'error': '工具未注册'}, status=400)

    # 两步确认：白名单声明需确认且尚未携 token
    if SUGGESTION_TOOLS[tool_name] and not confirm_token:
        return JsonResponse({
            'ok': False,
            'need_confirm': True,
            'summary': str(body.get('summary') or '确认执行该操作？')[:200],
            'token': make_action_token(request.user, key or '-', 'confirm'),
        })
    if SUGGESTION_TOOLS[tool_name]:
        expected = make_action_token(request.user, key or '-', 'confirm')
        if not hmac.compare_digest(expected, confirm_token):
            return JsonResponse({'ok': False, 'error': '确认无效或已过期，请重试'}, status=400)

    # 执行：batch_status 按 target_ids 直达 apply，其余走工具主函数
    fn = tool['fn']
    if tool_name == 'activities.batch_status' and params.get('target_ids') and tool.get('apply'):
        fn = tool['apply']

    try:
        result = fn(request.user, params) or {}
    except ToolError as e:
        logger.warning('建议工具业务错误 user=%s tool=%s: %s', request.user.id, tool_name, e)
        return JsonResponse({'ok': False, 'error': str(e)})
    except Exception as e:
        logger.warning('建议工具执行失败 user=%s tool=%s: %s', request.user.id, tool_name, e)
        return JsonResponse({'ok': False, 'error': '执行失败，请稍后重试'})

    # 执行成功：标记已读（覆盖 dismissed 也合理，用户已操作）+ 失效缓存
    if key:
        SuggestionState.objects.update_or_create(
            user=request.user, fingerprint=key, defaults={'action': 'read'})
        invalidate_user_caches(request.user.id)

    reply = result.get('reply') or '操作完成'
    logger.info('建议工具执行成功 user=%s tool=%s key=%s', request.user.id, tool_name, key)
    return JsonResponse({'ok': True, 'reply': reply,
                         'changed': bool(result.get('changed'))})
