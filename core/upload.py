"""附件上传的公共约束。

大小上限只在 settings.ATTACHMENT_MAX_UPLOAD_SIZE 定义一次，
视图校验与模板提示统一从这里取，避免多处硬编码走偏。
"""
from django.conf import settings

MAX_UPLOAD_SIZE = getattr(settings, 'ATTACHMENT_MAX_UPLOAD_SIZE', 10 * 1024 * 1024)
MAX_UPLOAD_SIZE_MB = MAX_UPLOAD_SIZE // (1024 * 1024)
