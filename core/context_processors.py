from django.conf import settings


def qoder_context(request):
    """向模板注入 Qoder Cloud Agents 相关上下文"""
    return {
        'QODER_API_AVAILABLE': bool(settings.QODER_ACCESS_TOKEN and
                                     settings.QODER_ACCESS_TOKEN != 'your-qoder-access-token-here'),
    }


def site_brand(request):
    """向全站模板注入站点品牌名（导航 / 标题 / 登录页共用），改名只动 settings.SITE_NAME"""
    return {'SITE_NAME': settings.SITE_NAME}


def push_context(request):
    """向全站模板注入 Web Push 所需的开关与 VAPID 公钥

    - push_ready：未配置 VAPID 时前端入口直接隐藏（双保险，服务端也拒）；
    - 只需公钥，私钥永不出 settings/env。"""
    return {
        'push_ready': bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY),
        'vapid_public_key': settings.VAPID_PUBLIC_KEY,
    }


def expense_tag_suggestions(request):
    """向全站模板注入费用标签建议（快记面板 datalist 用）。

    登录前不查库；core.tags 延迟 import 避免应用启动期依赖。
    """
    if not request.user.is_authenticated:
        return {'expense_tag_suggestions': []}
    from .tags import tag_suggestions
    return {'expense_tag_suggestions': tag_suggestions('expense', request.user)}

