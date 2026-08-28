"""core 模板过滤器"""
import json
from urllib.parse import quote

from django import template

register = template.Library()


@register.filter
def json_url(value):
    """dict → URL 编码的 JSON 字符串

    用于把结构化参数放进 HTML data 属性（纯 ASCII，无引号/转义歧义），
    前端用 JSON.parse(decodeURIComponent(...)) 还原。
    """
    try:
        payload = json.dumps(value or {}, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError):
        payload = '{}'
    return quote(payload)
