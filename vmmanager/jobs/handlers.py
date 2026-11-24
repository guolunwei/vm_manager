"""各类任务的实现。

约定：

* 参数从 ``ctx.payload`` 里取，不依赖闭包；
* 每一步之间调 ``ctx.check_cancel()``，让「取消」能较快生效；
* 找不到对象时给出人话错误，不要把 ``Model.DoesNotExist`` 直接抛给用户。
"""

from __future__ import annotations

from django.utils import timezone

from vmmanager.models import (
    CloneType,
    IpLease,
    Job,
    JobKind,
    LeaseKind,
    OsFamily,
    VirtualMachine,
    VmTemplate,
)
from vmmanager.services import (
    clone,
    collector,
    disk,
    exporter,
    guest,
    inventory,
    network,
    power,
    provision,
    snapshot,
    vmrun,
    vmx,
    vnc,
)

from .base import JobContext
from .registry import register


def _get_vm(ctx: JobContext) -> VirtualMachine:
    vm_id = ctx.get("vm_id") or (ctx.job.vm_id if ctx.job else None)
    vm = VirtualMachine.objects.filter(pk=vm_id).first() if vm_id else None
    if vm is None:
        raise RuntimeError("Virtual machine not found (it may have been deleted)")
    return vm


def _get_template(ctx: JobContext) -> VmTemplate:
    template_id = ctx.get("template_id")
    template = VmTemplate.objects.filter(pk=template_id).first() if template_id else None
    if template is None:
        raise RuntimeError("Template not found")
    return template


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------
@register(JobKind.PROVISION)
def handle_provision(job: Job, ctx: JobContext) -> dict:
    template = _get_template(ctx)
    flavor = template.default_flavor
    if ctx.get("flavor_id"):
        from vmmanager.models import Flavor

        flavor = Flavor.objects.filter(pk=ctx.get("flavor_id")).first() or flavor

    cpus = int(ctx.get("cpus") or (flavor.cpus if flavor else template.default_cpus))
    memory_mb = int(ctx.get("memory_mb") or (flavor.memory_mb if flavor else template.default_memory_mb))

    spec = provision.ProvisionSpec(
        name=ctx.get("name"),
        template=template,
        cpus=cpus,
        memory_mb=memory_mb,
        ip=ctx.get("ip") or "",
        gateway=ctx.get("gateway") or template.gateway,
        dns=ctx.get("dns") or template.dns,
        clone_type=ctx.get("clone_type") or CloneType.LINKED,
        guest_password=ctx.get("guest_password") or "",
        hostname=ctx.get("hostname") or "",
        deploy_ssh_key=bool(ctx.get("deploy_ssh_key", True)),
        notes=ctx.get("notes") or "",
        owner=job.created_by,
        flavor=flavor,
    )

    ctx.check_cancel()
    vm = provision.provision(spec, ctx)

    # 把 job 关联到新建的机器，页面上就能从任务跳到详情
    Job.objects.filter(pk=job.pk).update(vm=vm, label=vm.name)
    job.vm = vm

    return {"vm_id": vm.pk, "name": vm.name, "ip": vm.ip_address}


# ---------------------------------------------------------------------------
# 电源
# ---------------------------------------------------------------------------
@register(JobKind.POWER_ON)
def handle_power_on(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    ctx.check_cancel()
    # VNC 配置必须在**关机状态**下写入：VMware 关机时会用内存里的状态
    # 重写 vmx，运行中追加的键直接被抹掉（实测踩过）。开机前补写正好。
    if vm.power_state != "running":
        try:
            vnc.ensure_config(vm, log=ctx.info)
        except Exception as exc:  # noqa: BLE001 — VNC 失败不该挡住开机
            ctx.warn(f"VNC config skipped: {exc}")
    power.power_on(vm, ctx, gui=bool(ctx.get("gui", False)))
    return {"vm_id": vm.pk, "state": vm.power_state}


@register(JobKind.POWER_OFF)
def handle_power_off(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    ctx.check_cancel()
    power.power_off(vm, ctx, force=bool(ctx.get("force", False)))
    return {"vm_id": vm.pk, "state": vm.power_state}


@register(JobKind.POWER_RESET)
def handle_power_reset(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    power.reset(vm, ctx)
    return {"vm_id": vm.pk, "state": vm.power_state}


@register(JobKind.SUSPEND)
def handle_suspend(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    power.suspend(vm, ctx)
    return {"vm_id": vm.pk, "state": vm.power_state}


@register(JobKind.DELETE)
def handle_delete(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    name = vm.name
    vm_id = vm.pk
    power.delete_vm(vm, ctx, remove_files=bool(ctx.get("remove_files", True)))
    # Job.vm 会因为 SET_NULL 变成 None，label 里已经存了名字
    return {"deleted": name, "vm_id": vm_id}


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
@register(JobKind.SET_IP)
def handle_set_ip(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    template = vm.template
    if template is None:
        raise RuntimeError("This VM has no associated template; cannot determine guest account")

    new_ip = (ctx.get("ip") or "").strip()
    if not new_ip:
        raise RuntimeError("No IP provided")
    prefix = int(ctx.get("prefix") or template.prefix_len)
    gateway = ctx.get("gateway") or template.gateway
    dns = ctx.get("dns") or template.dns

    # IP 冲突检查
    holder = IpLease.objects.filter(ip=new_ip).exclude(vm=vm).first()
    if holder is not None:
        raise RuntimeError(f"IP {new_ip} is already in use ({holder.vm or holder.note or 'unknown'})")

    if not vmrun.is_running(vm.vmx_path):
        raise RuntimeError("VM is not powered on; power it on before configuring the IP")

    password = ctx.get("guest_password") or template.guest_password
    session = guest.GuestSession(vm.vmx_path, template.guest_user, password)

    ctx.info("Waiting for guest to accept commands")
    if not session.wait_ready(timeout=120):
        raise RuntimeError("Guest unavailable; check whether open-vm-tools is running")

    ctx.info(f"Changing address of {vm.name} to {new_ip}/{prefix}")
    old_ip = vm.ip_address
    try:
        session.set_static_ip(
            new_ip,
            prefix=prefix,
            gateway=gateway,
            connection=template.guest_connection,
            dns=dns,
        )
    except guest.GuestError as exc:
        ctx.warn(f"nmcli returned an error (common when switching IP): {exc}")

    reachable = network.wait_port(new_ip, 22, timeout=75) or network.ping(new_ip)
    if reachable:
        ctx.ok(f"{new_ip} is reachable")
        if old_ip and old_ip != new_ip:
            IpLease.release(old_ip)
        IpLease.reserve(new_ip, vm=vm, mac=vm.mac_address, kind=LeaseKind.STATIC, note="手动配置")
        VirtualMachine.objects.filter(pk=vm.pk).update(
            ip_address=new_ip,
            ip_checked_at=timezone.now(),
        )
    else:
        ctx.warn(
            f"{new_ip} not reachable yet — the network may still be restarting; the address has been recorded"
        )
        if old_ip and old_ip != new_ip:
            IpLease.release(old_ip)
        IpLease.reserve(new_ip, vm=vm, mac=vm.mac_address, kind=LeaseKind.STATIC, note="手动配置")
        VirtualMachine.objects.filter(pk=vm.pk).update(
            ip_address=new_ip,
            ip_checked_at=timezone.now(),
        )

    return {"vm_id": vm.pk, "ip": new_ip, "reachable": reachable}


@register(JobKind.PROBE_IP)
def handle_probe_ip(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    ip = inventory.probe_ip(vm, ctx, allow_scan=bool(ctx.get("allow_scan", True)))
    return {"vm_id": vm.pk, "ip": ip}


# ---------------------------------------------------------------------------
# 巡检
# ---------------------------------------------------------------------------
@register(JobKind.SYNC_INVENTORY)
def handle_sync(job: Job, ctx: JobContext) -> dict:
    ctx.check_cancel()
    result = inventory.sync_inventory(ctx)
    ctx.check_cancel()
    if ctx.get("refresh", True):
        inventory.refresh_all(ctx)
    return result


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
@register(JobKind.SNAPSHOT_CREATE)
def handle_snapshot_create(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    snap = snapshot.create(
        vm,
        ctx.get("name"),
        ctx,
        description=ctx.get("description") or "",
        user=job.created_by,
    )
    return {"vm_id": vm.pk, "snapshot": snap.name}


@register(JobKind.SNAPSHOT_REVERT)
def handle_snapshot_revert(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    snapshot.revert(vm, ctx.get("name"), ctx)
    return {"vm_id": vm.pk, "snapshot": ctx.get("name")}


@register(JobKind.SNAPSHOT_DELETE)
def handle_snapshot_delete(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    snapshot.delete(vm, ctx.get("name"), ctx)
    return {"vm_id": vm.pk, "snapshot": ctx.get("name")}


# ---------------------------------------------------------------------------
# 重配
# ---------------------------------------------------------------------------
@register(JobKind.RECONFIGURE)
def handle_reconfigure(job: Job, ctx: JobContext) -> dict:
    vm = _get_vm(ctx)
    provision.reconfigure_guest(vm, ctx, guest_password=ctx.get("guest_password") or "")
    return {"vm_id": vm.pk}


# ---------------------------------------------------------------------------
# 规格调整
# ---------------------------------------------------------------------------
@register(JobKind.RESIZE)
def handle_resize(job: Job, ctx: JobContext) -> dict:
    """调整已有虚拟机的 vCPU / 内存。

    VMware 只在**开机时**读取 vmx 里的规格，运行中改文件无效；所以流程是
    「在跑就先关机 → 改 vmx → 原来在跑就再开机」。关机用的是软关机，
    等不到 Tools 会按 SOFT_STOP_GRACE 强制断电（与页面上的「关机」一致）。
    """
    vm = _get_vm(ctx)
    if not vm.exists:
        raise RuntimeError(f"vmx not found on disk: {vm.vmx_path}")

    cpus = int(ctx.get("cpus") or 0)
    memory_mb = int(ctx.get("memory_mb") or 0)
    if not 1 <= cpus <= 128:
        raise RuntimeError(f"vCPU must be between 1 and 128, got {cpus}")
    if not 256 <= memory_mb <= 1048576:
        raise RuntimeError(f"Memory must be between 256 and 1048576 MB, got {memory_mb}")
    if memory_mb % 4:
        raise RuntimeError("Memory must be a multiple of 4 MB")

    was_running = vmrun.is_running(vm.vmx_path)
    ctx.check_cancel()

    if was_running:
        ctx.info("VM is running; powering off to apply the new spec")
        power.power_off(vm, ctx, force=bool(ctx.get("force", False)))
        ctx.check_cancel()

    ctx.info(f"Applying spec {cpus} vCPU / {memory_mb} MB to vmx")
    try:
        vmx.patch(
            vm.vmx_path,
            {
                "memSize": memory_mb,
                "numvcpus": cpus,
                "cpuid.coresPerSocket": vmx.cpu_topology(cpus),
            },
        )
    except (vmx.VmxError, OSError) as exc:
        raise RuntimeError(f"Failed to write vmx: {exc}") from exc
    ctx.ok("Spec written to vmx")

    flavor_id = ctx.get("flavor_id")
    updates: dict = {"cpus": cpus, "memory_mb": memory_mb}
    if flavor_id:
        from vmmanager.models import Flavor

        flavor = Flavor.objects.filter(pk=flavor_id).first()
        if flavor is not None:
            updates["flavor"] = flavor
    VirtualMachine.objects.filter(pk=vm.pk).update(**updates)

    if was_running:
        ctx.info("Powering the VM back on")
        power.power_on(vm, ctx)

    ctx.ok(f"Resize completed: {cpus} vCPU / {memory_mb} MB")
    return {
        "vm_id": vm.pk,
        "cpus": cpus,
        "memory_mb": memory_mb,
        "restarted": was_running,
    }


# ---------------------------------------------------------------------------
# 磁盘扩容
# ---------------------------------------------------------------------------
@register(JobKind.GROW_DISK)
def handle_grow_disk(job: Job, ctx: JobContext) -> dict:
    """扩容虚拟磁盘。

    前置：必须关机（vdiskmanager 要求）、必须是完整克隆（链接克隆的磁盘
    是模板的 overlay，扩不动）。扩完磁盘后若源机有 Linux 模板，会自动开机
    并尝试 growpart + resize2fs/xfs_growfs——尽力而为，失败不影响磁盘扩容。
    """
    vm = _get_vm(ctx)
    if not vm.exists:
        raise RuntimeError(f"vmx not found on disk: {vm.vmx_path}")
    if vm.clone_type == CloneType.LINKED:
        raise RuntimeError(
            "Linked-clone disks share the template's base disk and cannot be resized. "
            "Make a full clone of this VM first, then resize that disk"
        )

    grow_gb = int(ctx.get("grow_gb") or 0)
    size_gb = int(ctx.get("size_gb") or 0)
    if grow_gb < 0 or size_gb < 0 or (grow_gb == 0 and size_gb == 0):
        raise RuntimeError("Provide grow_gb (GB to add) or size_gb (absolute target)")

    if vmrun.is_running(vm.vmx_path):
        raise RuntimeError("VM must be powered off before resizing its disk; power it off first")

    current_gb = disk.disk_capacity_gb(vm.vmx_path)
    ctx.info(f"Current disk capacity: ~{current_gb} GB")
    if size_gb:
        target_gb = size_gb
    else:
        import math

        target_gb = math.ceil(current_gb) + grow_gb
    if target_gb <= current_gb:
        raise RuntimeError(
            f"Target size {target_gb} GB is not larger than the current ~{current_gb} GB. "
            "Note: for split/sparse disks the parsed capacity may be a lower bound — "
            "use size_gb to set an absolute target"
        )

    disk.grow_disk(vm.vmx_path, target_gb, ctx.info)
    ctx.ok(f"Disk resized to {target_gb} GB")

    # Guest 内扩分区：尽力而为。有 Linux 模板就开机扩展，否则保持关机，
    # 用户可以之后自己开机处理。
    template = vm.template
    expanded = False
    if template is not None and template.os_family == OsFamily.LINUX:
        ctx.info("Powering on to grow the partition/filesystem inside the guest")
        try:
            power.power_on(vm, ctx)
        except Exception as exc:  # noqa: BLE001 — 扩容已完成，开机失败只警告
            ctx.warn(f"Power-on failed; the guest partition is NOT grown yet: {exc}")
            return {"vm_id": vm.pk, "size_gb": target_gb, "fs_grown": False}

        password = ctx.get("guest_password") or template.guest_password
        session = guest.GuestSession(vm.vmx_path, template.guest_user, password)
        if not session.wait_ready(timeout=120):
            ctx.warn("Guest not ready; the partition is NOT grown yet. Retry later or run growpart manually")
            return {"vm_id": vm.pk, "size_gb": target_gb, "fs_grown": False}

        ctx.info("Growing partition and filesystem inside guest (best-effort)")
        expanded = disk.grow_filesystem(session, ctx.info)
        if expanded:
            ctx.ok("Filesystem grown")
        else:
            ctx.warn("Filesystem growth skipped (see log above); the disk itself IS resized")

    return {"vm_id": vm.pk, "size_gb": target_gb, "fs_grown": expanded}


# ---------------------------------------------------------------------------
# 克隆虚拟机
# ---------------------------------------------------------------------------
@register(JobKind.CLONE_VM)
def handle_clone_vm(job: Job, ctx: JobContext) -> dict:
    """完整克隆一台已有虚拟机（VM → VM，带磁盘内容）。"""
    source_id = ctx.get("source_vm_id") or (ctx.job.vm_id if ctx.job else None)
    source = VirtualMachine.objects.filter(pk=source_id).first() if source_id else None
    if source is None:
        raise RuntimeError("Source VM not found (it may have been deleted)")

    name = (ctx.get("name") or "").strip()
    if not name:
        raise RuntimeError("New VM name is required")

    vm = clone.clone_vm(
        source,
        name,
        ctx,
        ip=(ctx.get("ip") or "").strip(),
        cpus=int(ctx.get("cpus") or 0),
        memory_mb=int(ctx.get("memory_mb") or 0),
        notes=ctx.get("notes") or "",
        owner=job.created_by,
    )
    # 与 provision 相同：把任务关联到新机器，页面可以从任务跳到详情
    Job.objects.filter(pk=job.pk).update(vm=vm, label=vm.name)
    job.vm = vm
    return {"vm_id": vm.pk, "name": vm.name, "ip": vm.ip_address}


# ---------------------------------------------------------------------------
# 指标采集
# ---------------------------------------------------------------------------
@register(JobKind.METRICS)
def handle_metrics(job: Job, ctx: JobContext) -> dict:
    """Guest 内采集全部运行中机器的 CPU / 内存，写入时序表。

    由 collector 链式自续（METRICS_ENABLED 时自动排下一轮），
    也可以从 API 手动触发一轮。
    """
    return collector.run_collection(ctx)


@register(JobKind.EXPORT_OVF)
def handle_export_ovf(job: Job, ctx: JobContext) -> dict:
    """把虚拟机导出为 .ova（走 ovftool，要求关机）。

    ``EXPORT_DIR`` 未配置时落在 ``BASE_DIR/exports``；导出的绝对路径写进
    任务结果与日志，页面上可以直接看到文件在哪。
    """
    vm = _get_vm(ctx)
    return exporter.export_ova(vm, ctx)
