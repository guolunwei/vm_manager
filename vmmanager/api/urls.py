"""API 路由（``/api/v1/``）。"""

from __future__ import annotations

from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("vms", views.VirtualMachineViewSet, basename="vm")
router.register("templates", views.TemplateViewSet, basename="template")
router.register("flavors", views.FlavorViewSet, basename="flavor")
router.register("jobs", views.JobViewSet, basename="job")
router.register("leases", views.IpLeaseViewSet, basename="lease")
router.register("audit", views.AuditEventViewSet, basename="audit")

urlpatterns = [
    path("", include(router.urls)),
    # 用账号密码换一个长期 Token，方便脚本/CI 调用：POST {"username","password"}
    path("auth/token/", obtain_auth_token, name="api-token"),
    path("host/", views.host_info, name="api-host"),
    path("sync/", views.sync_inventory, name="api-sync"),
    path("probe-ips/", views.probe_all_ips, name="api-probe-ips"),
    path("subnets/", views.subnet_info, name="api-subnets"),
]
