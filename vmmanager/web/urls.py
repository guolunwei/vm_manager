"""页面路由。"""

from __future__ import annotations

from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("vms/create/", views.vm_create, name="vm_create"),
    path("vms/batch/", views.vm_batch, name="vm_batch"),
    path("vms/bulk/", views.vm_bulk_action, name="vm_bulk"),
    path("vms/<int:pk>/", views.vm_detail, name="vm_detail"),
    path("vms/<int:pk>/console/", views.vm_console, name="vm_console"),
    path("vms/<int:pk>/action/<str:action>/", views.vm_action, name="vm_action"),
    path("vms/<int:pk>/set-ip/", views.vm_set_ip, name="vm_set_ip"),
    path("vms/<int:pk>/resize/", views.vm_resize, name="vm_resize"),
    path("vms/<int:pk>/clone/", views.vm_clone, name="vm_clone"),
    path("vms/<int:pk>/grow-disk/", views.vm_grow_disk, name="vm_grow_disk"),
    path("vms/<int:pk>/export/", views.vm_export, name="vm_export"),
    path("vms/<int:pk>/snapshots/create/", views.snapshot_create, name="snapshot_create"),
    path("vms/<int:pk>/snapshots/<str:name>/revert/", views.snapshot_revert, name="snapshot_revert"),
    path("vms/<int:pk>/snapshots/<str:name>/delete/", views.snapshot_delete, name="snapshot_delete"),
    path("jobs/", views.jobs_list, name="jobs"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/cancel/", views.job_cancel, name="job_cancel"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/templates/<int:pk>/power/", views.template_power, name="template_power"),
    path("settings/templates/<int:pk>/delete/", views.template_delete, name="template_delete"),
    path("settings/flavors/<int:pk>/delete/", views.flavor_delete, name="flavor_delete"),
    path("network/", views.network_view, name="network"),
    path("network/forwards/add/", views.network_forward_add, name="network_forward_add"),
    path("network/forwards/delete/", views.network_forward_delete, name="network_forward_delete"),
    path("network/bindings/add/", views.network_binding_add, name="network_binding_add"),
    path("network/bindings/delete/", views.network_binding_delete, name="network_binding_delete"),
    path("audit/", views.audit_list, name="audit"),
]
