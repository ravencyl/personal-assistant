"""core 模板过滤器"""
import hashlib
import json
import os
from urllib.parse import quote

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

# 静态资源版本号缓存：path → (源文件 (mtime_ns, size), 哈希串)。
# 开发机改一下文件 mtime 就变，不需要重启服务。
_STATIC_TOKENS = {}


def source_token(source_path):
    """源文件内容前 10 位 md5（只读文件，不碰 mtime，便于单测）"""
    with open(source_path, 'rb') as fh:
        return hashlib.md5(fh.read()).hexdigest()[:10]


def static_versioned(path):
    """{% staticv 'css/custom.css' %} → /static/css/custom.css?v=<内容哈希>

    为什么必需：nginx 的 location /static/ 不下发任何 Cache-Control（只有
    etag / last-modified），浏览器于是按「启发式新鲜度」（约 (now - Last-Modified)
    × 10%）直接复用磁盘里的旧文件，连网络都不走；Service Worker 的 network-first
    用的就是这条 fetch()，同样会被 HTTP 缓存答回来。真实故障：全站两列化上线后，
    详情页拿到新 HTML + 旧 CSS，.page-cols 没有 grid 声明就退化成块级流，
    右列（快捷操作·附件·参与者）整块掉到页底。

    拿内容哈希而不是手动升版本号：改了文件 URL 自动变，手动号一忘就是又一场
    「发布后样式滞后」。找不列源文件（未 collectstatic 等）则退回裸 URL，
    宁可不加版本号也不能渲染报错。
    """
    url = static(path)
    try:
        source = finders.find(path)
        if not source:
            return url
        stat = os.stat(source)
        cache_key = (stat.st_mtime_ns, stat.st_size)
        cached = _STATIC_TOKENS.get(path)
        if cached and cached[0] == cache_key:
            token = cached[1]
        else:
            token = source_token(source)
            _STATIC_TOKENS[path] = (cache_key, token)
    except OSError:
        return url
    return f"{url}{'&' if '?' in url else '?'}v={token}"


@register.filter
def ai_markdown(value):
    """AI / 用户写的 Markdown → 安全 HTML（实现见 core/markdown_render.py）

    返回 SafeString，模板里不需要再写 |safe。先转义再解析，所以模型输出的
    任何 HTML 只能以文字形态出现；解析失败会降级成纯文本段落。
    """
    from core.markdown_render import render_markdown
    return render_markdown(value)


@register.simple_tag
def staticv(path):
    """带内容版本号的 static URL（本地手写 CSS/JS 专用）"""
    return static_versioned(path)


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
