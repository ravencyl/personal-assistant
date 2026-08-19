from django.conf import settings


def qoder_context(request):
    """向模板注入 Qoder Cloud Agents 相关上下文"""
    return {
        'QODER_API_AVAILABLE': bool(settings.QODER_ACCESS_TOKEN and
                                     settings.QODER_ACCESS_TOKEN != 'your-qoder-access-token-here'),
    }


def app_info(request):
    """应用基本信息"""
    return {
        'APP_NAME': 'Personal AI Assistant',
        'APP_VERSION': '1.0.0',
    }
