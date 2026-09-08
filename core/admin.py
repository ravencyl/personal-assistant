from django.contrib import admin
from django.conf import settings


# 自定义 Admin 站点标题（品牌名统一取 settings.SITE_NAME）
admin.site.site_header = f'{settings.SITE_NAME} 管理后台'
admin.site.site_title = settings.SITE_NAME
admin.site.index_title = '管理面板'
