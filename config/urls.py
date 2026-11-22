"""URL 总入口。"""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "VM Manager 后台"
admin.site.site_title = "VM Manager"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("vmmanager.api.urls")),
    path("", include("vmmanager.web.urls")),
]

if settings.DEBUG:
    # 开发环境直接由 Django 提供静态文件
    from django.conf.urls.static import static

    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
