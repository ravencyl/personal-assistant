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

