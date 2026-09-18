"""一句话快速输入：AI 解析（失败降级规则解析）与快速创建端点"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core.ai import ai_round_trip, extract_json_dict
from core.utils import week_monday, WEEKDAY_LABELS

from ..parsing import parse_quick_input
from ..services import create_activity_from_parsed
from ..utils import normalize_input
from ._common import _participant_skip_text

logger = logging.getLogger(__name__)

# 图片上传大小限制（与 settings.ATTACHMENT_MAX_UPLOAD_SIZE 对齐）
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/heic'}


@login_required
@ensure_csrf_cookie
@require_POST
def parse_quick_input_view(request):
    """快速输入解析：AI 优先（Qoder general agent），失败/未配置时降级规则解析"""
    text = (request.POST.get('text') or '').strip()
    if not text:
        return JsonResponse({'error': '请输入内容'}, status=400)
    if len(text) > 500:
        return JsonResponse({'error': '输入过长，请控制在 500 字以内'}, status=400)

    today = timezone.localdate()
    data = normalize_input(_ai_parse(text, today) or {}, today)
    source = 'ai' if data.get('name') else 'rule'
    if source == 'rule':
        data = normalize_input(parse_quick_input(text, today), today)
    if not data.get('name'):
        return JsonResponse({
            'error': '未能识别出活动名称，请写得更具体些，例如「8月25到28日去上海出差 预算3000」',
        }, status=400)
    data['source'] = source
    return JsonResponse(data)


@login_required
@require_POST
def activity_quick_create(request):
    """列表页快速创建（快速输入预览卡片确认；一律创建为顶级活动）"""
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': '请求数据格式错误'}, status=400)
    data = normalize_input(data, timezone.localdate())
    if not data.get('name'):
        return JsonResponse({'error': '活动名称不能为空'}, status=400)

    result = create_activity_from_parsed(request.user, data, source='快速输入')
    activity = result['activity']

    return JsonResponse({
        'id': activity.id,
        'name': activity.name,
        'url': reverse('activities:activity_detail', args=[activity.id]),
        'note': _participant_skip_text(result['skipped']),
    })


def _week_anchor_text(today):
    """生成给模型看的日历对照表（周一为一周起点）

    模型自己推周基准时会错开整周（实测把周日的「下周五」推到了下下周五）。
    直接把上周/本周/下周的绝对日期列出来比任何文字约定都稳定，
    口径与 parsing.parse_quick_input 的周解析一致（包括「上周X」往回取）。
    早于今天的日期逐格标「已过去」：只靠文字约定「顺延」时模型仍会把活动排到今天
    （实测周日的「周六聚餐」返回 08-30），标在单元格上才能驱动它改用下周同一天。
    """
    monday = week_monday(today)

    def cell(offset, i):
        d = monday + timedelta(days=offset + i)
        return f'{WEEKDAY_LABELS[i]}={d.isoformat()}' + ('（已过去）' if d < today else '')

    return (f'今天是 {today.isoformat()}（{WEEKDAY_LABELS[today.weekday()]}）。'
            '自然周从周一开始、周日结束（周日属于当前这一周，不是下一周的开始）。'
            '相对日期必须照下面的日期表换算，不要自己推断周基准：\n'
            f'上周：{",".join(cell(-7, i) for i in range(7))}\n'
            f'本周：{",".join(cell(0, i) for i in range(7))}\n'
            f'下周：{",".join(cell(7, i) for i in range(7))}\n'
            '「下周X」取下一行，「上周X」取上一行；没写前缀的裸「周X」取本周，'
            '但本周那行标了「已过去」的同星期日不可用，改用下周那行同一天。'
            '往回看的说法（昨天/前天/N天前/上周X）按字面取那个过去日期——'
            '补记的花费必须落在花钱那天；除此之外 start_date 不得早于今天。')


def _ai_parse(text, today):
    """调用 Qoder general agent 解析快速输入；未配置/超时/异常时返回 None（由调用方降级）"""
    if not settings.QODER_ACCESS_TOKEN:
        return None
    try:
        prompt = (
            f'从用户输入中提取活动记录的字段，只返回一个 JSON 对象（不要解释、不要 markdown 代码块）。\n'
            f'{_week_anchor_text(today)}\n'
            '字段：name（活动名称，字符串）、start_date、end_date（YYYY-MM-DD，相对日期如明天/昨天/上周六/月底/下周五请换算为绝对日期，未写年份用当年）、'
            'start_time、end_time（HH:MM 24 小时制，如 14:00、15:30；用户写「下午3点」换算为 15:00、「上午9点半」换算为 09:30，只识别有明确上下午/词头或钟表格式的时间，未写时间则不出现在 JSON 中）、'
            'cost（数字，单位元，指已经花掉的钱）、'
            'status（planned/in_progress/done/cancelled 之一）、'
            'tags（字符串数组）、participants（字符串数组）。\n'
            f'无法识别的字段不要出现在 JSON 中。用户输入："""{text}"""'
        )
        return extract_json_dict(ai_round_trip(prompt, timeout=20, purpose='general'))
    except Exception as e:
        logger.warning(f'快速输入 AI 解析失败，将降级规则解析: {e}')
        return None


@login_required
@require_POST
def ocr_receipt_view(request):
    """OCR 收据/发票识别：接收图片，提取金额/日期/类别等信息

    当前实现：将图片转为 base64 编码，发送给 AI 进行解析。
    如果 AI 不支持视觉输入，返回错误并建议手动输入。
    失败时返回 400，前端降级为手动输入表单。
    """
    image = request.FILES.get('image')
    if not image:
        return JsonResponse({'error': '请上传图片'}, status=400)

    # 验证文件类型
    if image.content_type not in ALLOWED_IMAGE_TYPES:
        return JsonResponse({'error': '不支持的图片格式，请使用 JPG/PNG/WebP'}, status=400)

    # 验证文件大小
    if image.size > MAX_IMAGE_SIZE:
        return JsonResponse({'error': f'图片过大，请控制在 {MAX_IMAGE_SIZE // 1024 // 1024}MB 以内'}, status=400)

    # 读取图片并尝试 OCR
    try:
        image_bytes = image.read()
        result = _ocr_receipt_ai(image_bytes)
        if not result:
            return JsonResponse({
                'error': '未能识别图片内容，请手动输入',
                'fallback': True,
            }, status=400)
        return JsonResponse(result)
    except Exception as e:
        logger.warning(f'OCR 识别失败: {e}')
        return JsonResponse({
            'error': '图片识别失败，请手动输入',
            'fallback': True,
        }, status=400)


def _ocr_receipt_ai(image_bytes):
    """调用 AI 视觉能力识别收据/发票内容

    当前 AI 服务不支持直接图片输入，返回 None 由调用方降级。
    未来可在 AI 服务支持视觉输入时扩展此函数。
    """
    # TODO: 当 AI 服务支持视觉输入时，实现图片解析
    # 目前返回 None，前端降级为手动输入
    logger.info('OCR 视觉输入暂未实现，降级为手动输入')
    return None
