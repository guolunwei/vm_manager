"""REST API。

设计原则：

* 所有**变更类**操作都返回 ``202 + job_id``，由 worker 异步执行，不在请求里阻塞；
* 查询类接口直接返回当前数据库状态；
* 日志走 ``/jobs/{id}/log/?offset=N`` 增量拉取，避免每次传全量。

用 Token 认证（``Authorization: Token <key>``）方便脚本集成，
页面本身仍用 Session。
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from vmmanager.jobs import queue
from vmmanager.models import (
    AuditAction,
    AuditEvent,
    Flavor,
    IpLease,
    Job,
    JobKind,
    JobStatus,
    PowerState,
    QuotaExceeded,
    VirtualMachine,
    VmTemplate,
    check_quota_for_create,
)
from vmmanager.services import batch, collector, exporter, metrics, network

from .serializers import (
    AuditEventSerializer,
    FlavorSerializer,
    IpLeaseSerializer,
    JobLogSerializer,
    JobSerializer,
    SnapshotSerializer,
    TemplateSerializer,
    VirtualMachineSerializer,
)


def _job_response(job: Job) -> Response:
    return Response(
        {"job_id": job.pk, "kind": job.kind, "status": job.status, "label": job.label},
        status=status.HTTP_202_ACCEPTED,
    )


def _enqueue(request, kind: str, *, payload: dict, vm=None, label: str = "", dedupe: str = ""):
    try:
        job = queue.enqueue(
            kind,
            payload=payload,
            vm=vm,
            label=label,
            user=request.user,
            dedupe_key=dedupe,
        )
    except queue.DuplicateJob as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

    AuditEvent.record(
        actor=request.user,
        action=AuditAction.API,
        target=vm,
        target_label=label or (vm.name if vm else ""),
        detail={"kind": kind, "job": job.pk},
        request=request,
    )
    return _job_response(job)


class VirtualMachineViewSet(viewsets.ModelViewSet):
    queryset = VirtualMachine.objects.select_related("template", "flavor", "owner")
    serializer_class = VirtualMachineSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = []
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        keyword = (params.get("q") or "").strip()
        if keyword:
            queryset = queryset.filter(
                Q(name__icontains=keyword) | Q(hostname__icontains=keyword) | Q(ip_address__icontains=keyword)
            )
        state = params.get("state")
        if state in PowerState.values:
            queryset = queryset.filter(power_state=state)
        template = params.get("template")
        if template and template.isdigit():
            queryset = queryset.filter(template_id=int(template))
        return queryset

    def create(self, request, *args, **kwargs):
        """``POST /vms/`` 不是直接建记录，而是入队一个创建任务。"""
        if not (request.user.is_staff or settings.ALLOW_SELF_SERVICE):
            return Response({"detail": "没有创建权限"}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        template_id = data.get("template")
        template = VmTemplate.objects.filter(pk=template_id).first()
        if template is None:
            return Response({"detail": "template 不存在"}, status=status.HTTP_400_BAD_REQUEST)

        name = (data.get("name") or "").strip()
        if not name:
            return Response({"detail": "name 必填"}, status=status.HTTP_400_BAD_REQUEST)

        memory_mb = data.get("memory_mb")
        if not memory_mb and data.get("flavor"):
            flavor = Flavor.objects.filter(pk=data.get("flavor")).first()
            memory_mb = flavor.memory_mb if flavor else None
        try:
            memory_mb = int(memory_mb) if memory_mb else 0
        except (TypeError, ValueError):
            memory_mb = 0
        try:
            check_quota_for_create(request.user, count=1, memory_mb_total=memory_mb)
        except QuotaExceeded as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        payload = {
            "name": name,
            "template_id": template.pk,
            "flavor_id": data.get("flavor") or None,
            "cpus": data.get("cpus"),
            "memory_mb": data.get("memory_mb"),
            "ip": data.get("ip_address") or "",
            "clone_type": data.get("clone_type") or "linked",
            "notes": data.get("notes") or "",
            "deploy_ssh_key": bool(data.get("deploy_ssh_key", True)),
            "guest_password": data.get("guest_password") or "",
        }
        return _enqueue(
            request,
            JobKind.PROVISION,
            payload=payload,
            label=name,
            dedupe=f"provision:{name.lower()}",
        )

    def destroy(self, request, *args, **kwargs):
        vm = self.get_object()
        if not request.user.is_staff:
            return Response({"detail": "只有管理员可以删除"}, status=status.HTTP_403_FORBIDDEN)
        return _enqueue(
            request,
            JobKind.DELETE,
            payload={"vm_id": vm.pk, "remove_files": True},
            vm=vm,
            dedupe=f"delete:{vm.pk}",
        )

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        """批量动作：``POST /vms/bulk/ {"action": "start", "ids": [1,2,3]}``。

        逐台独立入队，返回按台的成败明细，HTTP 状态码只表达请求本身是否合法。
        """
        action_name = str(request.data.get("action") or "")
        if action_name not in batch.BULK_ACTIONS:
            return Response(
                {"detail": f"不支持的操作：{action_name}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_ids = request.data.get("ids")
        ids: list[int] = []
        if isinstance(raw_ids, list):
            for item in raw_ids:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
        ids = list(dict.fromkeys(ids))[:50]
        if not ids:
            return Response({"detail": "ids 必填"}, status=status.HTTP_400_BAD_REQUEST)

        if action_name == "delete" and not request.user.is_staff:
            return Response(
                {"detail": "只有管理员可以删除"},
                status=status.HTTP_403_FORBIDDEN,
            )

        result = batch.bulk_action(action_name, ids, user=request.user)
        if result["jobs"]:
            AuditEvent.record(
                actor=request.user,
                action=AuditAction.DELETE if action_name == "delete" else AuditAction.API,
                target_label=f"批量操作 {len(result['jobs'])} 台",
                detail={"action": action_name, "ids": ids, "jobs": [j["job_id"] for j in result["jobs"]]},
                request=request,
            )
        return Response(
            {
                "action": action_name,
                "jobs": result["jobs"],
                "errors": result["errors"],
                "queued": len(result["jobs"]),
                "failed": len(result["errors"]),
            }
        )

    # -- 电源 --------------------------------------------------------------
    @action(detail=True, methods=["post"], url_path="power/(?P<op>on|off|reset|suspend)")
    def power(self, request, pk=None, op=None):
        vm = self.get_object()
        mapping = {
            "on": JobKind.POWER_ON,
            "off": JobKind.POWER_OFF,
            "reset": JobKind.POWER_RESET,
            "suspend": JobKind.SUSPEND,
        }
        kind = mapping.get(op)
        if kind is None:
            return Response({"detail": "不支持的操作"}, status=status.HTTP_400_BAD_REQUEST)
        payload = {"vm_id": vm.pk}
        if op == "off" and request.data.get("force"):
            payload["force"] = True
        return _enqueue(request, kind, payload=payload, vm=vm, dedupe=f"{kind}:{vm.pk}")

    # -- 网络 --------------------------------------------------------------
    @action(detail=True, methods=["post"], url_path="set-ip")
    def set_ip(self, request, pk=None):
        vm = self.get_object()
        ip = (request.data.get("ip") or "").strip()
        if not ip:
            return Response({"detail": "ip 必填"}, status=status.HTTP_400_BAD_REQUEST)
        holder = IpLease.objects.filter(ip=ip).exclude(vm=vm).first()
        if holder:
            return Response({"detail": f"IP {ip} 已被占用"}, status=status.HTTP_409_CONFLICT)
        return _enqueue(
            request,
            JobKind.SET_IP,
            payload={
                "vm_id": vm.pk,
                "ip": ip,
                "prefix": request.data.get("prefix"),
                "gateway": request.data.get("gateway") or "",
                "dns": request.data.get("dns") or "",
                "guest_password": request.data.get("guest_password") or "",
            },
            vm=vm,
            dedupe=f"set_ip:{vm.pk}",
        )

    @action(detail=True, methods=["post"], url_path="resize")
    def resize(self, request, pk=None):
        """调整规格。运行中的机器会先关机再开机；返回 202 + job_id。"""
        vm = self.get_object()
        try:
            cpus = int(request.data.get("cpus"))
            memory_mb = int(request.data.get("memory_mb"))
        except (TypeError, ValueError):
            return Response(
                {"detail": "cpus 与 memory_mb 必须是整数"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not 1 <= cpus <= 128:
            return Response({"detail": "cpus 必须在 1-128 之间"}, status=status.HTTP_400_BAD_REQUEST)
        if not 256 <= memory_mb <= 1048576:
            return Response(
                {"detail": "memory_mb 必须在 256-1048576 之间"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return _enqueue(
            request,
            JobKind.RESIZE,
            payload={
                "vm_id": vm.pk,
                "cpus": cpus,
                "memory_mb": memory_mb,
                "force": bool(request.data.get("force", False)),
            },
            vm=vm,
            label=f"{vm.name} → {cpus}C/{memory_mb}M",
            dedupe=f"resize:{vm.pk}",
        )

    @action(detail=True, methods=["post"], url_path="clone")
    def clone(self, request, pk=None):
        """完整克隆一台虚拟机。``{"name": "web-02", "ip": "可选"}`` → 202 + job_id。"""
        if not (request.user.is_staff or settings.ALLOW_SELF_SERVICE):
            return Response({"detail": "没有创建权限"}, status=status.HTTP_403_FORBIDDEN)
        vm = self.get_object()
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "name 必填"}, status=status.HTTP_400_BAD_REQUEST)

        memory_mb = request.data.get("memory_mb") or vm.memory_mb or 0
        try:
            check_quota_for_create(request.user, count=1, memory_mb_total=int(memory_mb))
        except (QuotaExceeded, TypeError, ValueError) as exc:
            return Response(
                {"detail": str(exc) if isinstance(exc, QuotaExceeded) else "memory_mb 无效"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return _enqueue(
            request,
            JobKind.CLONE_VM,
            payload={
                "source_vm_id": vm.pk,
                "name": name,
                "ip": request.data.get("ip") or "",
                "cpus": request.data.get("cpus"),
                "memory_mb": request.data.get("memory_mb"),
            },
            vm=vm,
            label=f"{vm.name} → {name}",
            dedupe=f"clone:{vm.pk}:{name.lower()}",
        )

    @action(detail=True, methods=["post"], url_path="grow-disk")
    def grow_disk(self, request, pk=None):
        """扩容磁盘。``{"grow_gb": 10}`` 或 ``{"size_gb": 60}``。必须关机。"""
        vm = self.get_object()

        def _int(key):
            try:
                return int(request.data.get(key) or 0)
            except (TypeError, ValueError):
                return -1

        grow_gb, size_gb = _int("grow_gb"), _int("size_gb")
        if (grow_gb <= 0 and size_gb <= 0) or (grow_gb > 0 and size_gb > 0):
            return Response(
                {"detail": "grow_gb 与 size_gb 必须且只能提供一个"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return _enqueue(
            request,
            JobKind.GROW_DISK,
            payload={"vm_id": vm.pk, "grow_gb": max(grow_gb, 0), "size_gb": max(size_gb, 0)},
            vm=vm,
            label=f"{vm.name} 磁盘扩容",
            dedupe=f"grow_disk:{vm.pk}",
        )

    @action(detail=True, methods=["post"], url_path="probe-ip")
    def probe_ip(self, request, pk=None):
        vm = self.get_object()
        return _enqueue(
            request,
            JobKind.PROBE_IP,
            payload={"vm_id": vm.pk},
            vm=vm,
            dedupe=f"probe_ip:{vm.pk}",
        )

    @action(detail=True, methods=["get"], url_path="metrics")
    def metrics_series(self, request, pk=None):
        """Guest 内采集的时序指标。``?hours=24`` 控制回看窗口（上限 168）。"""
        vm = self.get_object()
        try:
            hours = min(max(int(request.query_params.get("hours", 24)), 1), 168)
        except (TypeError, ValueError):
            hours = 24
        series = collector.metric_series(vm, hours=hours)
        return Response({"vm": vm.pk, "hours": hours, "count": len(series), "series": series})

    @action(detail=True, methods=["post"], url_path="export")
    def export_ova(self, request, pk=None):
        """导出为 .ova。要求虚拟机关机且宿主装了 ovftool。"""
        vm = self.get_object()
        if not (request.user.is_staff or settings.ALLOW_SELF_SERVICE):
            return Response({"detail": "没有导出权限"}, status=status.HTTP_403_FORBIDDEN)
        if not exporter.ovftool_path():
            return Response(
                {"detail": "ovftool not found; set OVFTOOL in config.ini"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return _enqueue(
            request,
            JobKind.EXPORT_OVF,
            payload={"vm_id": vm.pk},
            vm=vm,
            label=f"{vm.name} 导出 OVA",
            dedupe=f"export:{vm.pk}",
        )

    @action(detail=True, methods=["post"], url_path="reconfigure")
    def reconfigure(self, request, pk=None):
        vm = self.get_object()
        return _enqueue(
            request,
            JobKind.RECONFIGURE,
            payload={"vm_id": vm.pk, "guest_password": request.data.get("guest_password") or ""},
            vm=vm,
            dedupe=f"reconfigure:{vm.pk}",
        )

    # -- 快照 --------------------------------------------------------------
    @action(detail=True, methods=["get", "post"], url_path="snapshots")
    def snapshots(self, request, pk=None):
        vm = self.get_object()
        if request.method == "GET":
            return Response(SnapshotSerializer(vm.snapshots.all(), many=True).data)

        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "name 必填"}, status=status.HTTP_400_BAD_REQUEST)
        return _enqueue(
            request,
            JobKind.SNAPSHOT_CREATE,
            payload={"vm_id": vm.pk, "name": name, "description": request.data.get("description") or ""},
            vm=vm,
            label=f"{vm.name}@{name}",
            dedupe=f"snapshot:{vm.pk}",
        )

    @action(detail=True, methods=["post"], url_path=r"snapshots/(?P<name>[^/]+)/revert")
    def snapshot_revert(self, request, pk=None, name=None):
        vm = self.get_object()
        return _enqueue(
            request,
            JobKind.SNAPSHOT_REVERT,
            payload={"vm_id": vm.pk, "name": name},
            vm=vm,
            label=f"{vm.name}@{name}",
            dedupe=f"snapshot:{vm.pk}",
        )

    @action(detail=True, methods=["delete"], url_path=r"snapshots/(?P<name>[^/]+)")
    def snapshot_delete(self, request, pk=None, name=None):
        vm = self.get_object()
        if not request.user.is_staff:
            return Response({"detail": "只有管理员可以删除快照"}, status=status.HTTP_403_FORBIDDEN)
        return _enqueue(
            request,
            JobKind.SNAPSHOT_DELETE,
            payload={"vm_id": vm.pk, "name": name},
            vm=vm,
            label=f"{vm.name}@{name}",
            dedupe=f"snapshot:{vm.pk}",
        )


class TemplateViewSet(viewsets.ModelViewSet):
    queryset = VmTemplate.objects.select_related("default_flavor").order_by("name")
    serializer_class = TemplateSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.request.method in ("POST", "PUT", "PATCH", "DELETE"):
            return [IsAdminUser()]
        return [IsAuthenticated()]


class FlavorViewSet(viewsets.ModelViewSet):
    queryset = Flavor.objects.order_by("sort_order", "memory_mb")
    serializer_class = FlavorSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.request.method in ("POST", "PUT", "PATCH", "DELETE"):
            return [IsAdminUser()]
        return [IsAuthenticated()]


class JobViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Job.objects.select_related("vm", "created_by", "template")
    serializer_class = JobSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        state = params.get("status")
        if state in JobStatus.values:
            queryset = queryset.filter(status=state)
        kind = params.get("kind")
        if kind in JobKind.values:
            queryset = queryset.filter(kind=kind)
        vm_id = params.get("vm")
        if vm_id and vm_id.isdigit():
            queryset = queryset.filter(vm_id=int(vm_id))
        return queryset

    @action(detail=True, methods=["get"], url_path="log")
    def log(self, request, pk=None):
        """增量日志。前端带上次拿到的 ``offset`` 即可。"""
        job = self.get_object()
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0

        text = job.log or ""
        # offset 超过当前长度（任务被重置）时从头给
        if offset > len(text):
            offset = 0

        payload = {
            "id": job.pk,
            "status": job.status,
            "status_display": job.get_status_display(),
            "progress": job.progress,
            "offset": len(text),
            "chunk": text[offset:],
            "error": job.error or "",
            "finished": job.is_finished,
            "duration": job.duration,
        }
        return Response(JobLogSerializer(payload).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        job = self.get_object()  # 走一遍权限与存在性检查
        if not queue.request_cancel(job.pk, user=request.user):
            return Response({"detail": "任务已结束，无法取消"}, status=status.HTTP_409_CONFLICT)
        return Response({"ok": True})

    @action(detail=True, methods=["post"], url_path="retry")
    def retry(self, request, pk=None):
        """重试失败 / 已取消的任务：按原参数入队一条新任务。"""
        job = self.get_object()
        try:
            new_job = queue.retry_job(job, user=request.user)
        except queue.NotRetryable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except queue.DuplicateJob as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"ok": True, "job_id": new_job.pk, "retried_from": job.pk})


class IpLeaseViewSet(viewsets.ModelViewSet):
    queryset = IpLease.objects.select_related("vm").order_by("ip")
    serializer_class = IpLeaseSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.request.method in ("POST", "PUT", "PATCH", "DELETE"):
            return [IsAdminUser()]
        return [IsAuthenticated()]


class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditEvent.objects.select_related("actor")
    serializer_class = AuditEventSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        queryset = super().get_queryset()
        action_name = self.request.query_params.get("action")
        if action_name in AuditAction.values:
            queryset = queryset.filter(action=action_name)
        return queryset


# ---------------------------------------------------------------------------
# 功能接口
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_info(request):
    """主机资源与整体概况。"""
    data = metrics.host_metrics()
    data["pending_jobs"] = queue.pending_count()
    data["templates"] = VmTemplate.objects.filter(is_active=True).count()
    data["base_dir"] = settings.VMM.get("BASE_DIR")
    data["vmrun"] = settings.VMM.get("VMRUN")
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def sync_inventory(request):
    """入队一次「同步磁盘」。"""
    return _enqueue(
        request,
        JobKind.SYNC_INVENTORY,
        payload={"refresh": True},
        label="同步磁盘",
        dedupe="sync_inventory",
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def probe_all_ips(request):
    """入队一次批量 IP 探测（用 sync 任务的刷新分支实现）。"""
    return _enqueue(
        request,
        JobKind.SYNC_INVENTORY,
        payload={"refresh": True},
        label="探测全部 IP",
        dedupe="sync_inventory",
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def subnet_info(request):
    """列出模板对应的网段与占用情况。"""
    result = []
    for template in VmTemplate.objects.filter(is_active=True):
        if not template.gateway:
            continue
        cidr = network.subnet_of(template.gateway, template.prefix_len)
        if not cidr:
            continue
        leases = [
            {"ip": str(lease.ip), "mac": lease.mac, "vm": lease.vm.name if lease.vm else None}
            for lease in IpLease.objects.select_related("vm").all()
            if str(lease.ip).startswith(cidr.rsplit(".", 1)[0] + ".")
        ]
        result.append(
            {
                "cidr": cidr,
                "gateway": template.gateway,
                "template": template.name,
                "used": len(leases),
                "leases": leases,
            }
        )
    return Response(result)
