"""API 序列化器。"""

from __future__ import annotations

from rest_framework import serializers

from vmmanager.models import (
    AuditEvent,
    Flavor,
    IpLease,
    Job,
    Snapshot,
    VirtualMachine,
    VmTemplate,
)


class FlavorSerializer(serializers.ModelSerializer):
    spec_display = serializers.CharField(read_only=True)

    class Meta:
        model = Flavor
        fields = ["id", "name", "cpus", "memory_mb", "spec_display", "description", "sort_order", "is_active"]


class TemplateSerializer(serializers.ModelSerializer):
    exists = serializers.BooleanField(read_only=True)
    snapshot_label = serializers.CharField(read_only=True)
    default_flavor = FlavorSerializer(read_only=True)
    default_flavor_id = serializers.PrimaryKeyRelatedField(
        source="default_flavor",
        queryset=Flavor.objects.all(),
        allow_null=True,
        required=False,
        write_only=True,
    )

    class Meta:
        model = VmTemplate
        fields = [
            "id",
            "name",
            "vmx_path",
            "snapshot",
            "snapshot_label",
            "exists",
            "os_family",
            "network_mode",
            "guest_user",
            "guest_password",
            "guest_connection",
            "prefix_len",
            "gateway",
            "dns",
            "default_cpus",
            "default_memory_mb",
            "default_flavor",
            "default_flavor_id",
            "deploy_ssh_key",
            "is_active",
            "notes",
            "created_at",
        ]
        read_only_fields = ["created_at"]
        extra_kwargs = {"guest_password": {"write_only": True, "required": False}}


class SnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = Snapshot
        fields = ["id", "name", "description", "size_bytes", "is_current", "created_at"]
        read_only_fields = ["id", "created_at", "size_bytes", "is_current"]


class IpLeaseSerializer(serializers.ModelSerializer):
    vm_name = serializers.CharField(source="vm.name", read_only=True, default=None)

    class Meta:
        model = IpLease
        fields = ["id", "ip", "mac", "kind", "note", "vm", "vm_name", "created_at"]
        read_only_fields = ["id", "created_at"]


class VirtualMachineSerializer(serializers.ModelSerializer):
    spec_display = serializers.CharField(read_only=True)
    ip_display = serializers.CharField(read_only=True)
    state_css = serializers.CharField(read_only=True)
    template_name = serializers.CharField(source="template.name", read_only=True, default=None)
    flavor_name = serializers.CharField(source="flavor.name", read_only=True, default=None)
    owner_name = serializers.CharField(source="owner.username", read_only=True, default=None)
    exists = serializers.BooleanField(read_only=True)

    class Meta:
        model = VirtualMachine
        fields = [
            "id",
            "name",
            "hostname",
            "vmx_path",
            "exists",
            "template",
            "template_name",
            "flavor",
            "flavor_name",
            "owner_name",
            "cpus",
            "memory_mb",
            "spec_display",
            "clone_type",
            "ip_address",
            "ip_display",
            "mac_address",
            "power_state",
            "state_css",
            "tools_state",
            "state_checked_at",
            "ip_checked_at",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "vmx_path",
            "power_state",
            "tools_state",
            "mac_address",
            "state_checked_at",
            "ip_checked_at",
            "created_at",
            "updated_at",
        ]


class JobSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    vm_name = serializers.CharField(source="vm.name", read_only=True, default=None)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default=None)
    duration = serializers.IntegerField(read_only=True)

    class Meta:
        model = Job
        fields = [
            "id",
            "kind",
            "kind_display",
            "status",
            "status_display",
            "label",
            "vm",
            "vm_name",
            "template",
            "progress",
            "log",
            "error",
            "result",
            "created_by_name",
            "queued_at",
            "started_at",
            "finished_at",
            "duration",
            "worker_id",
            "attempts",
            "cancel_requested",
        ]
        read_only_fields = fields


class JobLogSerializer(serializers.Serializer):
    """日志增量响应，前端按 offset 轮询。"""

    id = serializers.IntegerField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    progress = serializers.IntegerField()
    offset = serializers.IntegerField()
    chunk = serializers.CharField()
    error = serializers.CharField()
    finished = serializers.BooleanField()
    duration = serializers.IntegerField()


class AuditEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor_label", read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "actor_name",
            "action",
            "action_display",
            "target_type",
            "target_id",
            "target_label",
            "detail",
            "source_ip",
            "path",
            "created_at",
        ]
