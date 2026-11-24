"""页面视图。

约定：**视图不执行耗时操作**。所有克隆 / 开关机 / 改 IP / 快照动作都只是
``enqueue`` 一个任务然后返回 job id，前端轮询进度。这样刷新页面、关掉浏览器、
甚至重启 Web 进程都不会让操作半途而废。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from vmmanager.jobs import queue
from vmmanager.models import (
    AuditAction,
    AuditEvent,
    CloneType,
    Flavor,
    IpLease,
    Job,
    JobKind,
    JobStatus,
    PowerState,
    QuotaExceeded,
    UserQuota,
    VirtualMachine,
    VmTemplate,
    Worker,
    check_quota_for_create,
    usage_for,
)
from vmmanager.services import batch, collector, disk, exporter, metrics, natconf, network, vmrun, vnc

from .forms import (
    CloneForm,
    FlavorForm,
    GrowDiskForm,
    LoginForm,
    ResizeForm,
    SetIpForm,
    SnapshotForm,
    TemplateForm,
)

logger = logging.getLogger("vmmanager.web")

staff_required = user_passes_test(
    lambda u: u.is_authenticated and u.is_staff,
    login_url="login",
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _json(data: dict, status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status)


def _error(message: str, status: int = 400) -> JsonResponse:
    return _json({"ok": False, "error": message}, status)


def _can_create(user) -> bool:
    return bool(user.is_authenticated and (user.is_staff or settings.ALLOW_SELF_SERVICE))


def _enqueue(
    request, kind: str, *, payload: dict, vm=None, label: str = "", dedupe: str = ""
) -> JsonResponse:
    """统一的入队响应。"""
    # 记录入队时前面还有多少任务，前端可以提示「已排在 N 个任务之后」
    ahead = queue.pending_count_for_vm(vm) if vm is not None else 0

    # 允许排队，但不能无限堆：一台机器上几十个任务谁也说不清会执行成什么样
    limit = settings.VMM.get("MAX_PENDING_PER_VM", 6)
    if vm is not None and ahead >= limit:
        return _error(
            f"这台虚拟机已有 {ahead} 个任务排队，超过上限（{limit}）。"
            "请等待其完成，或在任务页取消不需要的任务。",
            status=429,
        )

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
        return _error(str(exc), status=409)

    request._audited = True
    AuditEvent.record(
        actor=request.user,
        action=AuditAction.POWER if kind.startswith(("power", "suspend")) else AuditAction.API,
        target=vm,
        target_label=label or (vm.name if vm else ""),
        detail={"kind": kind, "job": job.pk, "payload": _safe_payload(payload)},
        request=request,
    )
    return _json(
        {
            "ok": True,
            "job_id": job.pk,
            "status": job.status,
            "label": job.label,
            **({"queued_behind": ahead} if ahead else {}),
        }
    )


def _safe_payload(payload: dict) -> dict:
    """审计里不要把密码写进去。"""
    return {k: ("***" if "password" in k else v) for k, v in payload.items()}


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password"],
        )
        if user is not None:
            login(request, user)
            request._audited = True
            AuditEvent.record(
                actor=user,
                action=AuditAction.LOGIN,
                target_label=user.username,
                request=request,
            )
            return redirect(request.GET.get("next") or "dashboard")

        AuditEvent.record(
            actor=None,
            action=AuditAction.LOGIN,
            target_label=form.cleaned_data["username"],
            detail={"success": False},
            request=request,
        )
        request._audited = True
        form.add_error(None, "用户名或密码错误")

    return render(request, "login.html", {"form": form})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        request._audited = True
        AuditEvent.record(
            actor=request.user,
            action=AuditAction.LOGOUT,
            target_label=request.user.username,
            request=request,
        )
    logout(request)
    return redirect("login")


# ---------------------------------------------------------------------------
# 仪表盘
# ---------------------------------------------------------------------------
@login_required
def dashboard(request):
    queryset = VirtualMachine.objects.select_related("template", "flavor", "owner")
    # 行内显示「还有几个任务在排队」，让用户知道按钮点下去之后发生了什么
    queryset = queryset.annotate(
        pending_jobs=Count("jobs", filter=~Q(jobs__status__in=JobStatus.finished()), distinct=True),
    ).order_by("-created_at")  # annotate 会冲掉 Meta.ordering，分页必须显式排序

    keyword = (request.GET.get("q") or "").strip()
    if keyword:
        queryset = queryset.filter(
            Q(name__icontains=keyword) | Q(hostname__icontains=keyword) | Q(ip_address__icontains=keyword)
        )

    state = request.GET.get("state") or ""
    if state in PowerState.values:
        queryset = queryset.filter(power_state=state)

    template_id = request.GET.get("template") or ""
    if template_id.isdigit():
        queryset = queryset.filter(template_id=int(template_id))

    counts = VirtualMachine.objects.aggregate(
        total=Count("id"),
        running=Count("id", filter=Q(power_state=PowerState.RUNNING)),
        stopped=Count("id", filter=Q(power_state=PowerState.STOPPED)),
        suspended=Count("id", filter=Q(power_state=PowerState.SUSPENDED)),
        unknown=Count("id", filter=Q(power_state=PowerState.UNKNOWN)),
        no_ip=Count("id", filter=Q(ip_address__isnull=True, power_state=PowerState.RUNNING)),
    )

    paginator = Paginator(queryset, 25)
    page = paginator.get_page(request.GET.get("page"))

    recent_jobs = Job.objects.select_related("vm").order_by("-id")[:8]
    active_jobs = Job.objects.exclude(status__in=JobStatus.finished()).order_by("id")

    # 模板也是虚拟机，但它不在 VirtualMachine 表里，统计上看不见。
    # 「宿主上有 1 台在跑，界面却说运行中 0」就是这么来的，这里必须显式暴露。
    template_list = list(VmTemplate.objects.filter(is_active=True).order_by("name"))
    try:
        running_vmx = vmrun.list_running_set()
    except Exception:
        running_vmx = set()
    running_templates = [t for t in template_list if t.exists and vmrun.normpath(t.vmx_path) in running_vmx]

    # 「任务一直排队中」最常见的真因是 worker 没起来。不显式提示的话，
    # 用户只能盯着进度条 0% 猜，完全不知道是进程没开。
    pending_jobs = Job.objects.filter(status=JobStatus.QUEUED).count()
    online_workers = Worker.online().count()
    worker_offline = pending_jobs > 0 and online_workers == 0

    context = {
        "page_obj": page,
        "vms": page.object_list,
        "counts": counts,
        "keyword": keyword,
        "state": state,
        "template_id": template_id,
        "templates": template_list,
        "running_templates": running_templates,
        "pending_jobs": pending_jobs,
        "online_workers": online_workers,
        "worker_offline": worker_offline,
        "recent_jobs": recent_jobs,
        "active_jobs": active_jobs,
        "host": metrics.host_metrics(),
        "can_create": _can_create(request.user),
        "states": PowerState.choices,
    }
    return render(request, "dashboard.html", context)


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------
@login_required
def vm_create(request):
    """新建虚拟机——单台与批量合一。

    一张表格，一行一台：填一行提交就是单个创建，加多行就是批量。
    GET 渲染表格页；POST 收 JSON（不是表单），逐行校验后独立入队。
    部分行校验失败不影响其他行——批量操作里「全有或全无」是最让人恼火的。
    """
    if not _can_create(request.user):
        messages.error(request, "你的账号没有创建虚拟机的权限")
        return redirect("dashboard")

    templates = list(
        VmTemplate.objects.filter(is_active=True).prefetch_related("default_flavor").order_by("name")
    )

    if request.method == "POST":
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error("请求格式无效")

        template = VmTemplate.objects.filter(
            pk=data.get("template_id"),
            is_active=True,
        ).first()
        if template is None:
            return _error("请选择可用的模板")

        defaults = _batch_defaults(data, template)
        templates_map = {t.pk: t for t in templates}
        outcome = batch.validate_rows(
            data.get("rows") or [],
            default_cpus=defaults["cpus"],
            default_memory_mb=defaults["memory_mb"],
            template_ids=set(templates_map),
        )
        if not outcome.rows:
            return _json(
                {
                    "ok": False,
                    "error": "没有可创建的虚拟机",
                    "errors": outcome.errors,
                    "jobs": [],
                }
            )

        try:
            result = batch.submit(
                outcome.rows,
                template=template,
                user=request.user,
                clone_type=defaults["clone_type"],
                deploy_ssh_key=defaults["deploy_ssh_key"],
                guest_password=defaults["guest_password"],
                notes=defaults["notes"],
                templates=templates_map,
            )
        except QuotaExceeded as exc:
            return _error(str(exc))
        result["errors"] = outcome.errors + result["errors"]

        if result["jobs"]:
            request._audited = True
            AuditEvent.record(
                actor=request.user,
                action=AuditAction.CREATE,
                target_label=f"创建 {len(result['jobs'])} 台虚拟机",
                detail={
                    "template": template.name,
                    "jobs": [job["job_id"] for job in result["jobs"]],
                    "names": [job["name"] for job in result["jobs"]],
                },
                request=request,
            )

        return _json(
            {
                "ok": bool(result["jobs"]),
                "jobs": result["jobs"],
                "errors": result["errors"],
                "created": len(result["jobs"]),
                "failed": len(result["errors"]),
            }
        )

    selected = None
    if (request.GET.get("template") or "").isdigit():
        selected = next(
            (t for t in templates if t.pk == int(request.GET["template"])),
            None,
        )
    selected = selected or (templates[0] if templates else None)

    return render(
        request,
        "vm_create.html",
        {
            "templates": templates,
            "templates_json": [
                {"id": t.pk, "name": t.name, "cpus": t.default_cpus, "memory": t.default_memory_mb}
                for t in templates
            ],
            "flavors_json": [
                {"name": f.name, "cpus": f.cpus, "memory": f.memory_mb}
                for f in Flavor.objects.filter(is_active=True).order_by("sort_order", "memory_mb")
            ],
            "selected": selected,
            "max_rows": batch.MAX_ROWS,
        },
    )


@login_required
def vm_batch(request):
    """兼容旧链接：批量创建已并入新建页。"""
    return redirect("vm_create")


@login_required
@require_POST
def vm_bulk_action(request):
    """批量操作：对勾选的多台虚拟机入队同一动作。

    请求体 ``{"action": "start|stop|hard-stop|reset|suspend|delete", "ids": [..]}``。
    与批量创建一致，逐台独立入队——一台冲突不影响其余，结果按台返回。
    """
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("请求格式无效")

    action = str(data.get("action") or "")
    if action not in batch.BULK_ACTIONS:
        return _error(f"未知操作：{action}")

    raw_ids = data.get("ids")
    ids: list[int] = []
    if isinstance(raw_ids, list):
        for item in raw_ids:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
    ids = list(dict.fromkeys(ids))[:50]  # 去重并设个安全上限
    if not ids:
        return _error("请先勾选要操作的虚拟机")

    if action == "delete" and not request.user.is_staff:
        return _error("只有管理员可以删除虚拟机", 403)

    result = batch.bulk_action(action, ids, user=request.user)

    if result["jobs"]:
        request._audited = True
        audit_action = AuditAction.DELETE if action == "delete" else AuditAction.POWER
        AuditEvent.record(
            actor=request.user,
            action=audit_action,
            target_label=f"批量{_action_label(action)} {len(result['jobs'])} 台",
            detail={
                "action": action,
                "names": [item["name"] for item in result["jobs"]],
                "jobs": [item["job_id"] for item in result["jobs"]],
            },
            request=request,
        )

    return _json(
        {
            "ok": bool(result["jobs"]),
            "action": action,
            "jobs": result["jobs"],
            "errors": result["errors"],
            "queued": len(result["jobs"]),
            "failed": len(result["errors"]),
        }
    )


def _action_label(action: str) -> str:
    return {
        "start": "开机",
        "stop": "关机",
        "hard-stop": "强制关机",
        "reset": "重启",
        "suspend": "挂起",
        "delete": "删除",
    }.get(action, action)


def _batch_defaults(data: dict, template: VmTemplate) -> dict:
    """把前端传来的公共配置收敛成一份干净的默认值。"""

    def _pick_int(key: str, fallback: int, low: int, high: int) -> int:
        try:
            value = int(data.get(key))
        except (TypeError, ValueError):
            return fallback
        return value if low <= value <= high else fallback

    clone_type = data.get("clone_type") or CloneType.LINKED
    if clone_type not in (CloneType.LINKED, CloneType.FULL):
        clone_type = CloneType.LINKED  # EXTERNAL 只能来自扫描，不能手动创建

    return {
        "cpus": _pick_int("cpus", template.default_cpus or 2, 1, 128),
        "memory_mb": _pick_int("memory_mb", template.default_memory_mb or 4096, 256, 1048576),
        "clone_type": clone_type,
        "deploy_ssh_key": data.get("deploy_ssh_key", True) is not False,
        "guest_password": str(data.get("guest_password") or "")[:128],
        "notes": str(data.get("notes") or "")[:500],
    }


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------
@login_required
def vm_console(request, pk: int):
    """noVNC 网页控制台。

    三种状态：
    - VM 没在运行：给出开机入口，VMware 的 VNC server 只在 VM 运行时监听
    - vmx 刚写入 VNC 配置（或此前未启用）：提示需要重启一次才生效
    - 一切就绪：签发短时效 token，页面里 noVNC 连 websockify
    """
    vm = get_object_or_404(
        VirtualMachine.objects.select_related("template", "flavor", "owner"),
        pk=pk,
    )
    if not vm.exists:
        messages.error(request, "这台虚拟机的 vmx 文件已不存在，无法打开控制台")
        return redirect("vm_detail", pk=pk)

    cfg = vnc.config_for(vm.vmx_path)
    if not (cfg["enabled"] and cfg["port"] and cfg["password"]):
        result = vnc.ensure_config(vm, log=lambda msg: messages.info(request, msg))
        cfg = result["config"]

    # ready 的判定不看"配置是不是刚写入"，而看 VMware 是否真的在监听——
    # 运行中写入的 vmx 要下次开机才生效，不管谁问都要如实回答"没起来"。
    mode = "ready"
    if not vm.is_running:
        mode = "offline"
    elif not vnc.is_listening(cfg["port"]):
        mode = "needs-restart"

    # 一键修复：VMware 关机时会用内存状态重写 vmx（运行中写入的 VNC 配置
    # 会被抹掉），面板上的「重启」也不会重读配置——只有完整关机再开机才行。
    # 开机处理器会在关机状态下自动补写 VNC 配置，所以这里只管排队。
    if request.method == "POST" and mode == "needs-restart":
        queue.enqueue(
            JobKind.POWER_OFF,
            payload={"vm_id": vm.pk, "force": True},
            vm=vm,
            label=vm.name,
            user=request.user,
            dedupe_key=f"power_off:{vm.pk}",
        )
        queue.enqueue(
            JobKind.POWER_ON,
            payload={"vm_id": vm.pk},
            vm=vm,
            label=vm.name,
            user=request.user,
            dedupe_key=f"power_on:{vm.pk}",
        )
        messages.success(request, "已排队：关机 → 开机（开机时会自动写入 VNC 配置）")
        return redirect("vm_detail", pk=pk)

    ws_url = ""
    if mode == "ready":
        token = vnc.issue_token(vm.vmx_path)
        host = request.get_host().split(":")[0]
        ws_url = f"ws://{host}:{settings.VMM['VNC_PROXY_PORT']}/{token}"

    return render(
        request,
        "vm_console.html",
        {
            "vm": vm,
            "mode": mode,
            "cfg": cfg,
            "ws_url": ws_url,
            "proxy_port": settings.VMM["VNC_PROXY_PORT"],
        },
    )


@login_required
def vm_detail(request, pk: int):
    vm = get_object_or_404(
        VirtualMachine.objects.select_related("template", "flavor", "owner"),
        pk=pk,
    )
    jobs = vm.jobs.select_related("created_by").order_by("-id")[:20]
    snapshots = vm.snapshots.order_by("-created_at")
    active_job = queue.active_job_for_vm(vm)
    leases = IpLease.objects.filter(vm=vm)

    set_ip_form = SetIpForm(
        initial={
            "ip": vm.ip_address or "",
            "prefix": vm.template.prefix_len if vm.template else 24,
            "gateway": vm.template.gateway if vm.template else "",
            "dns": vm.template.dns if vm.template else "",
        }
    )
    # 「新 IP」的示例值按模板网段推导，别写死某个网段的地址——
    # 换一台机器、换一个 VMnet，写死的示例就会误导人。
    hint = _ip_hint(vm.template)
    if hint:
        set_ip_form.fields["ip"].widget.attrs["placeholder"] = f"如 {hint}"

    try:
        disk_gb = disk.disk_capacity_gb(vm.vmx_path) if vm.exists else None
    except Exception:
        disk_gb = None

    # 导出信息：ovftool 在不在、导出目录在哪（不在页面上创建目录）
    ovftool_ok = bool(exporter.ovftool_path())
    export_dir_raw = settings.VMM.get("EXPORT_DIR") or str(Path(settings.VMM["BASE_DIR"]) / "exports")

    return render(
        request,
        "vm_detail.html",
        {
            "vm": vm,
            "jobs": jobs,
            "snapshots": snapshots,
            "active_job": active_job,
            "leases": leases,
            "metrics": metrics.vm_metrics(vm) if vm.is_running else {},
            "chart": collector.chart_data(vm),
            "metrics_enabled": bool(settings.VMM.get("METRICS_ENABLED")),
            "set_ip_form": set_ip_form,
            "resize_form": ResizeForm(initial={"cpus": vm.cpus, "memory_mb": vm.memory_mb}),
            "clone_form": CloneForm(),
            "grow_disk_form": GrowDiskForm(),
            "disk_gb": disk_gb,
            "ovftool_ok": ovftool_ok,
            "export_dir": export_dir_raw,
            "can_create": _can_create(request.user),
            "snapshot_form": SnapshotForm(),
            "can_manage": request.user.is_staff,
        },
    )


def _ip_hint(template) -> str:
    """给「新 IP」输入框编一个同网段的示例地址，取不到就返回空串。"""
    gateway = getattr(template, "gateway", "") or ""
    if not gateway:
        return ""
    try:
        network = ipaddress.ip_network(
            f"{gateway}/{getattr(template, 'prefix_len', 24) or 24}",
            strict=False,
        )
    except ValueError:
        return ""
    if network.num_addresses < 16:
        return ""
    # 第 10 个地址：避开网关(.1)和常见设备(.2)，又不会撞上 VMware 的 DHCP 池(.128+)
    return str(network.network_address + 10)


# ---------------------------------------------------------------------------
# 操作
# ---------------------------------------------------------------------------
_ACTION_KINDS = {
    "start": JobKind.POWER_ON,
    "stop": JobKind.POWER_OFF,
    "hard-stop": JobKind.POWER_OFF,
    "reset": JobKind.POWER_RESET,
    "suspend": JobKind.SUSPEND,
}


@login_required
@require_POST
def vm_action(request, pk: int, action: str):
    vm = get_object_or_404(VirtualMachine, pk=pk)

    # 这里**不再**因为「有未完成任务」而拒绝：那样一个卡在队列里的任务
    # 会让这台机器的所有按钮一起失效（用户会认为界面坏了）。
    # 动作按点击顺序排队执行，重复提交由 dedupe_key 拦住。

    if action in _ACTION_KINDS:
        payload = {"vm_id": vm.pk}
        if action == "hard-stop":
            payload["force"] = True
        return _enqueue(
            request,
            _ACTION_KINDS[action],
            payload=payload,
            vm=vm,
            dedupe=f"{_ACTION_KINDS[action]}:{vm.pk}",
        )

    if action == "delete":
        if not request.user.is_staff:
            return _error("只有管理员可以删除虚拟机", 403)
        remove_files = request.POST.get("remove_files", "1") in ("1", "true", "on")
        return _enqueue(
            request,
            JobKind.DELETE,
            payload={"vm_id": vm.pk, "remove_files": remove_files},
            vm=vm,
            dedupe=f"delete:{vm.pk}",
        )

    if action == "probe-ip":
        return _enqueue(
            request,
            JobKind.PROBE_IP,
            payload={"vm_id": vm.pk},
            vm=vm,
            dedupe=f"probe_ip:{vm.pk}",
        )

    if action == "reconfigure":
        return _enqueue(
            request,
            JobKind.RECONFIGURE,
            payload={"vm_id": vm.pk},
            vm=vm,
            dedupe=f"reconfigure:{vm.pk}",
        )

    return _error(f"未知操作：{action}", 404)


@login_required
@require_POST
def vm_resize(request, pk: int):
    """调整规格（vCPU / 内存）。

    VMware 只在开机时读 vmx，所以运行中的机器由任务先软关机、写完再开机。
    页面侧不做「必须关机」的硬校验——机器在不在跑只有执行时才知道，
    页面上的判断瞬间就会过期。
    """
    vm = get_object_or_404(VirtualMachine, pk=pk)
    if not vm.exists:
        return _error("这台虚拟机的 vmx 文件已不存在，无法调整规格")

    form = ResizeForm(request.POST)
    if not form.is_valid():
        return _error("; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))

    return _enqueue(
        request,
        JobKind.RESIZE,
        payload={
            "vm_id": vm.pk,
            "cpus": form.cleaned_data["cpus"],
            "memory_mb": form.cleaned_data["memory_mb"],
        },
        vm=vm,
        label=f"{vm.name} → {form.cleaned_data['cpus']}C/{form.cleaned_data['memory_mb']}M",
        dedupe=f"resize:{vm.pk}",
    )


@login_required
@require_POST
def vm_clone(request, pk: int):
    """完整克隆一台已有虚拟机（VM → VM，磁盘内容一并带走）。"""
    if not _can_create(request.user):
        return _error("你的账号没有创建虚拟机的权限", 403)

    source = get_object_or_404(VirtualMachine, pk=pk)
    if not source.exists:
        return _error("源虚拟机的 vmx 文件已不存在，无法克隆")

    form = CloneForm(request.POST)
    if not form.is_valid():
        return _error("; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))

    name = form.cleaned_data["name"]
    if name.lower() == source.name.lower():
        return _error("新名称不能与源机器相同（Windows 文件系统不区分大小写，会覆盖同一个目录）")
    try:
        check_quota_for_create(request.user, count=1, memory_mb_total=source.memory_mb or 0)
    except QuotaExceeded as exc:
        return _error(str(exc))
    return _enqueue(
        request,
        JobKind.CLONE_VM,
        payload={
            "source_vm_id": source.pk,
            "name": name,
            "ip": form.cleaned_data.get("ip") or "",
        },
        vm=source,
        label=f"{source.name} → {name}",
        dedupe=f"clone:{source.pk}:{name.lower()}",
    )


@login_required
@require_POST
def vm_grow_disk(request, pk: int):
    """扩容虚拟磁盘。机器必须关机；链接克隆不支持。"""
    vm = get_object_or_404(VirtualMachine, pk=pk)
    if not vm.exists:
        return _error("这台虚拟机的 vmx 文件已不存在")

    form = GrowDiskForm(request.POST)
    if not form.is_valid():
        return _error("; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))

    return _enqueue(
        request,
        JobKind.GROW_DISK,
        payload={
            "vm_id": vm.pk,
            "grow_gb": form.cleaned_data.get("grow_gb") or 0,
            "size_gb": form.cleaned_data.get("size_gb") or 0,
        },
        vm=vm,
        label=f"{vm.name} 磁盘扩容",
        dedupe=f"grow_disk:{vm.pk}",
    )


@login_required
@require_POST
def vm_export(request, pk: int):
    """导出虚拟机为 .ova。要求关机；ovftool 未安装时直接给出提示。"""
    if not _can_create(request.user):
        return _error("你的账号没有导出虚拟机的权限", 403)

    vm = get_object_or_404(VirtualMachine, pk=pk)
    if not vm.exists:
        return _error("这台虚拟机的 vmx 文件已不存在")

    if not exporter.ovftool_path():
        return _error(
            "未找到 ovftool（VMware Workstation 自带），无法导出。可在 config.ini 里用 OVFTOOL= 指定路径"
        )

    return _enqueue(
        request,
        JobKind.EXPORT_OVF,
        payload={"vm_id": vm.pk},
        vm=vm,
        label=f"{vm.name} 导出 OVA",
        dedupe=f"export:{vm.pk}",
    )


@login_required
@require_POST
def vm_set_ip(request, pk: int):
    vm = get_object_or_404(VirtualMachine, pk=pk)
    form = SetIpForm(request.POST)
    if not form.is_valid():
        return _error("; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))

    data = form.cleaned_data
    if IpLease.objects.filter(ip=data["ip"]).exclude(vm=vm).exists():
        return _error(f"IP {data['ip']} 已被其他虚拟机占用", 409)

    return _enqueue(
        request,
        JobKind.SET_IP,
        payload={
            "vm_id": vm.pk,
            "ip": data["ip"],
            "prefix": data.get("prefix") or (vm.template.prefix_len if vm.template else 24),
            "gateway": data.get("gateway") or "",
            "dns": data.get("dns") or "",
            "guest_password": data.get("guest_password") or "",
        },
        vm=vm,
        dedupe=f"set_ip:{vm.pk}",
    )


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
@login_required
@require_POST
def snapshot_create(request, pk: int):
    vm = get_object_or_404(VirtualMachine, pk=pk)
    form = SnapshotForm(request.POST)
    if not form.is_valid():
        return _error("; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))
    return _enqueue(
        request,
        JobKind.SNAPSHOT_CREATE,
        payload={
            "vm_id": vm.pk,
            "name": form.cleaned_data["name"],
            "description": form.cleaned_data.get("description") or "",
        },
        vm=vm,
        dedupe=f"snapshot:{vm.pk}",
    )


@login_required
@require_POST
def snapshot_revert(request, pk: int, name: str):
    vm = get_object_or_404(VirtualMachine, pk=pk)
    return _enqueue(
        request,
        JobKind.SNAPSHOT_REVERT,
        payload={"vm_id": vm.pk, "name": name},
        vm=vm,
        label=f"{vm.name}@{name}",
        dedupe=f"snapshot:{vm.pk}",
    )


@login_required
@require_POST
def snapshot_delete(request, pk: int, name: str):
    vm = get_object_or_404(VirtualMachine, pk=pk)
    if not request.user.is_staff:
        return _error("只有管理员可以删除快照", 403)
    return _enqueue(
        request,
        JobKind.SNAPSHOT_DELETE,
        payload={"vm_id": vm.pk, "name": name},
        vm=vm,
        label=f"{vm.name}@{name}",
        dedupe=f"snapshot:{vm.pk}",
    )


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
@login_required
def jobs_list(request):
    queryset = Job.objects.select_related("vm", "created_by", "template")

    status = request.GET.get("status") or ""
    if status in JobStatus.values:
        queryset = queryset.filter(status=status)

    kind = request.GET.get("kind") or ""
    if kind in JobKind.values:
        queryset = queryset.filter(kind=kind)

    paginator = Paginator(queryset, 30)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "jobs.html",
        {
            "page_obj": page,
            "jobs": page.object_list,
            "status": status,
            "kind": kind,
            "statuses": JobStatus.choices,
            "kinds": JobKind.choices,
        },
    )


@login_required
def job_detail(request, pk: int):
    job = get_object_or_404(Job.objects.select_related("vm", "created_by", "template"), pk=pk)
    return render(request, "job_detail.html", {"job": job})


@login_required
@require_POST
def job_cancel(request, pk: int):
    job = get_object_or_404(Job, pk=pk)
    if job.created_by_id and job.created_by_id != request.user.pk and not request.user.is_staff:
        return _error("只能取消自己发起的任务", 403)
    ok = queue.request_cancel(pk, user=request.user)
    if not ok:
        return _error("任务已结束，无法取消", 409)
    return _json({"ok": True, "job_id": pk})


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
@login_required
def settings_view(request):
    if request.method == "POST":
        if not request.user.is_staff:
            messages.error(request, "只有管理员可以修改设置")
            return redirect("settings")

        action = request.POST.get("action")
        if action == "template":
            instance = None
            if request.POST.get("id"):
                instance = get_object_or_404(VmTemplate, pk=request.POST["id"])
            form = TemplateForm(request.POST, instance=instance)
            if form.is_valid():
                saved = form.save()
                request._audited = True
                AuditEvent.record(
                    actor=request.user,
                    action=AuditAction.SETTINGS,
                    target=saved,
                    target_label=saved.name,
                    detail={"type": "template"},
                    request=request,
                )
                messages.success(request, f"模板 {saved.name} 已保存")
            else:
                messages.error(request, "模板保存失败：" + _format_errors(form))
        elif action == "flavor":
            instance = None
            if request.POST.get("id"):
                instance = get_object_or_404(Flavor, pk=request.POST["id"])
            form = FlavorForm(request.POST, instance=instance)
            if form.is_valid():
                saved = form.save()
                messages.success(request, f"规格 {saved.name} 已保存")
            else:
                messages.error(request, "规格保存失败：" + _format_errors(form))
        elif action == "quota":
            target_user = get_object_or_404(get_user_model(), pk=request.POST.get("user_id", ""))

            def _int_or_zero(raw: str) -> int:
                raw = (raw or "").strip()
                return max(0, int(raw)) if raw.isdigit() else 0

            quota_row, _created = UserQuota.objects.update_or_create(
                user=target_user,
                defaults={
                    "max_vms": _int_or_zero(request.POST.get("max_vms")),
                    "max_memory_mb": _int_or_zero(request.POST.get("max_memory_mb")),
                },
            )
            request._audited = True
            AuditEvent.record(
                actor=request.user,
                action=AuditAction.SETTINGS,
                target_label=target_user.username,
                detail={
                    "type": "quota",
                    "max_vms": quota_row.max_vms,
                    "max_memory_mb": quota_row.max_memory_mb,
                },
                request=request,
            )
            messages.success(request, f"{target_user.username} 的配额已保存（0 = 不设限）")
        return redirect("settings")

    edit_template = None
    if (request.GET.get("edit") or "").isdigit():
        edit_template = VmTemplate.objects.filter(pk=int(request.GET["edit"])).first()

    edit_flavor = None
    if (request.GET.get("edit_flavor") or "").isdigit():
        edit_flavor = Flavor.objects.filter(pk=int(request.GET["edit_flavor"])).first()

    template_list = list(VmTemplate.objects.select_related("default_flavor").order_by("name"))

    # 一次取回「宿主上正在运行的全部 vmx」，避免为每个模板各拉一个 vmrun 子进程
    try:
        running_vmx = vmrun.list_running_set()
    except Exception:
        running_vmx = set()
    running_template_ids = {
        t.pk for t in template_list if t.exists and vmrun.normpath(t.vmx_path) in running_vmx
    }

    return render(
        request,
        "settings.html",
        {
            "templates": template_list,
            "running_template_ids": running_template_ids,
            "flavors": Flavor.objects.order_by("sort_order", "memory_mb"),
            "leases": IpLease.objects.select_related("vm").order_by("ip"),
            "template_form": TemplateForm(instance=edit_template),
            "flavor_form": FlavorForm(instance=edit_flavor),
            "edit_template": edit_template,
            "edit_flavor": edit_flavor,
            # 配额管理：每个账号一行，带当前占用与上限（0 = 不设限）
            "quota_rows": [
                {
                    "user": user,
                    "quota": UserQuota.objects.filter(user=user).first(),
                    "usage": usage_for(user),
                }
                for user in get_user_model().objects.filter(is_active=True).order_by("username")
            ],
            "config": {
                "base_dir": settings.VMM.get("BASE_DIR"),
                "vmrun": settings.VMM.get("VMRUN"),
                "tools_wait": settings.VMM.get("TOOLS_WAIT"),
                "scan_enabled": settings.VMM.get("SCAN_ENABLED"),
                "allow_self_service": settings.ALLOW_SELF_SERVICE,
            },
            "can_manage": request.user.is_staff,
        },
    )


def _format_errors(form) -> str:
    return "; ".join(f"{field}: {', '.join(msgs)}" for field, msgs in form.errors.items())


@login_required
@require_POST
def template_power(request, pk: int):
    """模板的开关机。

    模板不属于 ``VirtualMachine``，所以不进任务队列——它是秒级的维护动作，
    页面需要立刻看到结果。默认策略：**模板应保持关机**，开机只为临时维护。
    """
    if not request.user.is_staff:
        messages.error(request, "只有管理员可以控制模板电源")
        return redirect("settings")

    template = get_object_or_404(VmTemplate, pk=pk)
    if not template.exists:
        messages.error(request, f"模板文件不存在：{template.vmx_path}")
        return redirect("settings")

    action = request.POST.get("action") or ""
    try:
        if action == "start":
            vmrun.start(template.vmx_path, gui=False)
            note = "已开机"
        elif action == "stop":
            vmrun.stop(template.vmx_path)
            note = "已关机"
        elif action == "stop-hard":
            vmrun.stop(template.vmx_path, hard=True)
            note = "已强制关机"
        else:
            messages.error(request, "未知的电源操作")
            return redirect("settings")
    except vmrun.VmrunError as exc:
        messages.error(request, f"模板 {template.name} 操作失败：{exc}")
        return redirect("settings")

    AuditEvent.record(
        actor=request.user,
        action=AuditAction.POWER,
        target=template,
        target_label=template.name,
        detail={"type": "template", "action": action},
        request=request,
    )
    request._audited = True
    messages.success(request, f"模板 {template.name} {note}")
    if action == "start":
        messages.warning(
            request, "模板已开机。维护完成后请记得关机；运行中的模板会让链接克隆的基础磁盘偏离快照。"
        )
    return redirect("settings")


@login_required
@require_POST
def template_delete(request, pk: int):
    if not request.user.is_staff:
        messages.error(request, "只有管理员可以删除模板")
        return redirect("settings")
    template = get_object_or_404(VmTemplate, pk=pk)
    name = template.name
    template.delete()
    AuditEvent.record(
        actor=request.user,
        action=AuditAction.DELETE,
        target_label=name,
        detail={"type": "template"},
        request=request,
    )
    request._audited = True
    messages.success(request, f"模板 {name} 已删除（已创建的虚拟机不受影响）")
    return redirect("settings")


@login_required
@require_POST
def flavor_delete(request, pk: int):
    if not request.user.is_staff:
        messages.error(request, "只有管理员可以删除规格")
        return redirect("settings")
    flavor = get_object_or_404(Flavor, pk=pk)
    name = flavor.name
    flavor.delete()
    messages.success(request, f"规格 {name} 已删除")
    return redirect("settings")


# ---------------------------------------------------------------------------
# 网络与审计
# ---------------------------------------------------------------------------
@login_required
@login_required
def network_view(request):
    templates = VmTemplate.objects.filter(is_active=True)
    all_leases = list(IpLease.objects.select_related("vm").order_by("ip"))
    # VMware 自己在 vmnetdhcp.conf 里写的网段信息，用来把「可用静态区」和
    # 「DHCP 动态池」区分开显示 —— 静态地址要避开动态池，否则迟早撞车。
    vmnet_by_cidr = {info["cidr"]: info for info in network.vmnet_networks().values()}

    subnets = []
    for template in templates:
        if not template.gateway:
            continue
        cidr = network.subnet_of(template.gateway, template.prefix_len)
        if not cidr:
            continue
        prefix = cidr.rsplit(".", 1)[0] + "."
        leases = [lease for lease in all_leases if str(lease.ip).startswith(prefix)]
        info = vmnet_by_cidr.get(cidr) or {}
        subnets.append(
            {
                "cidr": cidr,
                "template": template,
                "gateway": template.gateway,
                "dns": template.dns or info.get("dns", ""),
                "leases": leases,
                "used": len(leases),
                **_subnet_ranges(cidr, info.get("dhcp_pools") or []),
            }
        )

    return render(
        request,
        "network.html",
        {
            "subnets": subnets,
            "unassigned": [lease for lease in all_leases if lease.vm_id is None],
            "forwards": natconf.list_forwards(),
            "bindings": natconf.list_bindings(),
            "nat_conf_found": Path(natconf.nat_conf_path()).exists(),
            "dhcp_conf_found": Path(natconf.dhcp_conf_path()).exists(),
        },
    )


def _network_staff_guard(request) -> bool:
    """NAT / DHCP 改动会动到宿主网络，只允许管理员。"""
    if request.user.is_staff:
        return True
    messages.error(request, "只有管理员可以修改 NAT 转发与 DHCP 绑定")
    return False


@login_required
@require_POST
def network_forward_add(request):
    if not _network_staff_guard(request):
        return redirect("network")
    form_data = {
        "proto": (request.POST.get("proto") or "tcp").lower(),
        "host_port": request.POST.get("host_port") or "",
        "guest_ip": request.POST.get("guest_ip") or "",
        "guest_port": request.POST.get("guest_port") or "",
    }
    if form_data["proto"] not in ("tcp", "udp"):
        messages.error(request, "协议只支持 tcp / udp")
        return redirect("network")
    if not form_data["host_port"].isdigit() or not form_data["guest_port"].isdigit():
        messages.error(request, "端口号必须是数字")
        return redirect("network")
    try:
        ipaddress.ip_address(form_data["guest_ip"])
    except ValueError:
        messages.error(request, "虚拟机 IP 格式无效")
        return redirect("network")

    try:
        natconf.add_forward(
            form_data["proto"],
            int(form_data["host_port"]),
            form_data["guest_ip"],
            int(form_data["guest_port"]),
        )
    except natconf.NatConfError as exc:
        messages.error(request, f"转发添加失败：{exc}")
        return redirect("network")

    request._audited = True
    AuditEvent.record(
        actor=request.user,
        action=AuditAction.SETTINGS,
        target_label=f"NAT {form_data['proto']} {form_data['host_port']} → {form_data['guest_ip']}:{form_data['guest_port']}",
        detail={"type": "nat_forward", **form_data},
        request=request,
    )
    messages.success(request, "NAT 转发已添加并生效")
    return redirect("network")


@login_required
@require_POST
def network_forward_delete(request):
    if not _network_staff_guard(request):
        return redirect("network")
    proto = (request.POST.get("proto") or "").lower()
    host_port = request.POST.get("host_port") or ""
    guest_ip = request.POST.get("guest_ip") or ""
    guest_port = request.POST.get("guest_port") or ""
    if proto not in ("tcp", "udp") or not host_port.isdigit() or not guest_port.isdigit():
        messages.error(request, "转发条目参数无效")
        return redirect("network")

    try:
        natconf.remove_forward(proto, int(host_port), guest_ip, int(guest_port))
    except natconf.NatConfError as exc:
        messages.error(request, f"删除失败：{exc}")
        return redirect("network")

    request._audited = True
    AuditEvent.record(
        actor=request.user,
        action=AuditAction.SETTINGS,
        target_label=f"删除 NAT {proto} {host_port} → {guest_ip}:{guest_port}",
        detail={"type": "nat_forward_delete", "proto": proto, "host_port": host_port},
        request=request,
    )
    messages.success(request, "NAT 转发已删除并生效")
    return redirect("network")


@login_required
@require_POST
def network_binding_add(request):
    if not _network_staff_guard(request):
        return redirect("network")
    mac = (request.POST.get("mac") or "").strip()
    ip = (request.POST.get("ip") or "").strip()
    if not re.match(r"^[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){5}$", mac):
        messages.error(request, "MAC 地址格式无效（如 00:0C:29:AA:BB:CC）")
        return redirect("network")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        messages.error(request, "IP 格式无效")
        return redirect("network")

    try:
        natconf.add_dhcp_binding(mac, ip)
    except natconf.NatConfError as exc:
        messages.error(request, f"绑定失败：{exc}")
        return redirect("network")

    request._audited = True
    AuditEvent.record(
        actor=request.user,
        action=AuditAction.SETTINGS,
        target_label=f"DHCP 绑定 {mac} → {ip}",
        detail={"type": "dhcp_binding", "mac": mac, "ip": ip},
        request=request,
    )
    messages.success(request, "DHCP 静态绑定已添加并生效")
    return redirect("network")


@login_required
@require_POST
def network_binding_delete(request):
    if not _network_staff_guard(request):
        return redirect("network")
    mac = (request.POST.get("mac") or "").strip()
    try:
        natconf.remove_dhcp_binding(mac)
    except natconf.NatConfError as exc:
        messages.error(request, f"删除失败：{exc}")
        return redirect("network")

    request._audited = True
    AuditEvent.record(
        actor=request.user,
        action=AuditAction.SETTINGS,
        target_label=f"删除 DHCP 绑定 {mac}",
        detail={"type": "dhcp_binding_delete", "mac": mac},
        request=request,
    )
    messages.success(request, "DHCP 绑定已删除并生效")
    return redirect("network")


def _subnet_ranges(cidr: str, dhcp_pools: list[tuple[str, str]]) -> dict:
    """算出一个网段里「能给静态用」和「DHCP 会用」的地址区间，供页面展示。"""
    result = {"static_range": "", "dhcp_range": "", "capacity": 0}
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return result

    # 掐掉网络地址、广播地址，以及 .1/.2 这类网关位
    first = net.network_address + 3
    last = net.broadcast_address - 1

    if dhcp_pools:
        start, end = dhcp_pools[0]
        result["dhcp_range"] = f"{start} – {end}"
        try:
            last = ipaddress.ip_address(start) - 1
        except ValueError:
            pass

    if first <= last:
        result["static_range"] = f"{first} – {last}"
        # 容量只算真正能分给虚拟机的静态地址，别把 DHCP 池和网关位算进去
        result["capacity"] = int(last) - int(first) + 1
    return result


@login_required
def audit_list(request):
    if not request.user.is_staff:
        messages.error(request, "只有管理员可以查看审计日志")
        return redirect("dashboard")

    queryset = AuditEvent.objects.select_related("actor")
    action = request.GET.get("action") or ""
    if action in AuditAction.values:
        queryset = queryset.filter(action=action)

    paginator = Paginator(queryset, 50)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "audit.html",
        {
            "page_obj": page,
            "events": page.object_list,
            "action": action,
            "actions": AuditAction.choices,
        },
    )
