"""Django 后台（只用来管理用户与兜底数据修正，日常操作请走主界面）。"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    AuditEvent,
    Flavor,
    IpLease,
    Job,
    Snapshot,
    VirtualMachine,
    VmTemplate,
    Worker,
)


@admin.register(VmTemplate)
class VmTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "vmx_path", "snapshot", "os_family", "network_mode", "is_active")
    list_filter = ("os_family", "network_mode", "is_active")
    search_fields = ("name", "vmx_path")


@admin.register(Flavor)
class FlavorAdmin(admin.ModelAdmin):
    list_display = ("name", "cpus", "memory_mb", "is_active", "sort_order")
    list_editable = ("is_active", "sort_order")


@admin.register(VirtualMachine)
class VirtualMachineAdmin(admin.ModelAdmin):
    list_display = ("name", "ip_address", "power_state", "spec_display", "template", "owner", "created_at")
    list_filter = ("power_state", "template", "clone_type")
    search_fields = ("name", "hostname", "ip_address", "vmx_path")
    readonly_fields = ("created_at", "updated_at", "state_checked_at", "ip_checked_at")


@admin.register(Snapshot)
class SnapshotAdmin(admin.ModelAdmin):
    list_display = ("vm", "name", "is_current", "created_at")
    list_filter = ("is_current",)
    search_fields = ("name", "vm__name")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "label", "status", "progress", "created_by", "queued_at", "finished_at")
    list_filter = ("kind", "status")
    search_fields = ("label", "error")
    readonly_fields = ("log", "result", "created_at", "updated_at")


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ("name", "hostname", "pid", "current_job", "heartbeat_at", "started_at")
    readonly_fields = ("name", "hostname", "pid", "heartbeat_at", "started_at")


@admin.register(IpLease)
class IpLeaseAdmin(admin.ModelAdmin):
    list_display = ("ip", "mac", "vm", "kind", "note")
    list_filter = ("kind",)
    search_fields = ("ip", "mac")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor_label", "action", "target_label", "source_ip")
    list_filter = ("action",)
    search_fields = ("actor_label", "target_label")
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):  # 审计日志只读
        return False

    def has_change_permission(self, request, obj=None):
        return False
