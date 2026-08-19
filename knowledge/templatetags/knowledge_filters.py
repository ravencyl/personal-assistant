import markdown as md
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(name='markdown')
def markdown_filter(text):
    """将 Markdown 文本渲染为 HTML"""
    if not text:
        return ''
    html = md.markdown(
        str(text),
        extensions=['extra', 'nl2br', 'sane_lists'],
    )
    return mark_safe(html)
