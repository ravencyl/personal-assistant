"""建议交互端点：关闭/已读（JSON，由前端 fetch 消费）"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from core.models import SuggestionState

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
    cache.delete(f'suggestions_{request.user.id}')
    cache.delete(f'suggestion_states_{request.user.id}')
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
