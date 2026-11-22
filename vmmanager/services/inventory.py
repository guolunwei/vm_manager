"""纳管与巡检：把磁盘上的真实情况和数据库对齐。

三类工作：

* ``sync_inventory`` —— 扫描根目录，发现新 vmx 就纳管，记录里的文件没了就标失效；
* ``probe_ips`` —— 给运行中的虚拟机探测真实 IP 并回填（解决「页面 IP 是空的」）；
* ``refresh_all`` —— 状态 + 快照一次性刷新，供定时任务调用。
"""

from __future__ import annotations

import os

from django.conf import settings
from django.utils import timezone

from vmmanager.models import (
    CloneType,
    IpLease,
    LeaseKind,
    PowerState,
    VirtualMachine,
    VmTemplate,
    hostname_for,
)

from . import network, power, snapshot, vmrun, vmx
from .vmrun import VmrunError


def scan_vmx_files(base_dir: str | None = None, *, depth: int = 1) -> list[str]:
    """扫描虚拟机根目录下的 .vmx。

    默认只看一层子目录（``BASE_DIR/名字/名字.vmx``），这也是 VMware 的默认布局。
    """
    base = base_dir or settings.VMM["BASE_DIR"]
    if not base or not os.path.isdir(base):
        return []

    found: list[str] = []
    base_depth = os.path.normpath(base).rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        current_depth = os.path.normpath(root).count(os.sep) - base_depth
        if current_depth >= depth:
            dirs[:] = []
        # 跳过回收站之类的目录
        dirs[:] = [d for d in dirs if not d.startswith(("$", ".", "~"))]
        for filename in files:
            if filename.lower().endswith(".vmx"):
                found.append(os.path.join(root, filename))
    return sorted(found)


def sync_inventory(log) -> dict:
    """扫描磁盘并与数据库对齐。"""
    base = settings.VMM["BASE_DIR"]
    log.info(f"Scanning directory: {base}")
    if not base or not os.path.isdir(base):
        raise RuntimeError(f"VM root directory does not exist: {base}")

    paths = scan_vmx_files(base)
    log.info(f"Found {len(paths)} vmx files")

    try:
        running = vmrun.list_running_set()
    except VmrunError as exc:
        log.warn(f"Failed to read running list; power states may be inaccurate: {exc}")
        running = set()

    template_paths = {vmrun.normpath(p) for p in VmTemplate.objects.values_list("vmx_path", flat=True) if p}
    existing = {
        vmrun.normpath(vmx_path): vm for vm in VirtualMachine.objects.all() for vmx_path in [vm.vmx_path]
    }

    created, updated, skipped = 0, 0, 0

    for path in paths:
        norm = vmrun.normpath(path)
        if norm in template_paths:
            log.info(f"Skipping template: {os.path.basename(path)}")
            skipped += 1
            continue

        state = PowerState.RUNNING if norm in running else PowerState.STOPPED

        vm = existing.get(norm)
        if vm is not None:
            changes = {}
            if vm.power_state != state:
                changes["power_state"] = state
            changes["state_checked_at"] = timezone.now()
            # MAC 以 vmx 为准：VMware 首次开机时会重新生成 ethernet0.generatedAddress，
            # 数据库里开机前记下的那个值已经失效，按旧 MAC 去匹配 ARP 永远匹配不到。
            try:
                mac = vmx.mac_address(path)
                if mac and network.normalize_mac(mac) != network.normalize_mac(vm.mac_address):
                    changes["mac_address"] = mac
            except (vmx.VmxError, OSError):
                pass
            VirtualMachine.objects.filter(pk=vm.pk).update(**changes)
            updated += 1
            continue

        # 新发现：读 vmx 补齐规格
        try:
            info = vmx.summary(path)
        except (vmx.VmxError, OSError) as exc:
            log.warn(f"Failed to read, skipping {path}: {exc}")
            continue

        name = (
            info["display_name"]
            or os.path.basename(os.path.dirname(path))
            or os.path.splitext(os.path.basename(path))[0]
        )

        # 名字冲突时加后缀，避免因为一台机器阻塞整次同步
        candidate = name
        suffix = 2
        while VirtualMachine.objects.filter(name__iexact=candidate).exists():
            candidate = f"{name}-{suffix}"
            suffix += 1
        if candidate != name:
            log.warn(f"Name {name} is already taken, importing as {candidate}")

        vm = VirtualMachine.objects.create(
            name=candidate,
            hostname=hostname_for(candidate),
            vmx_path=path,
            cpus=info["cpus"],
            memory_mb=info["memory_mb"],
            mac_address=info["mac"],
            clone_type=CloneType.EXTERNAL,
            power_state=state,
            state_checked_at=timezone.now(),
            notes="由磁盘扫描自动导入",
        )
        created += 1
        log.ok(f"Imported {candidate} ({info['cpus']}C/{info['memory_mb']}MB)")

    # 记录还在，但文件没了
    missing = 0
    for vm in VirtualMachine.objects.exclude(vmx_path=""):
        if not vm.exists:
            missing += 1
            if vm.power_state != PowerState.UNKNOWN:
                VirtualMachine.objects.filter(pk=vm.pk).update(
                    power_state=PowerState.UNKNOWN,
                    state_checked_at=timezone.now(),
                )
                log.warn(f"{vm.name}: vmx no longer exists, marked as unknown")

    log.ok(
        f"Sync completed: {created} created, {updated} updated, {skipped} templates skipped, {missing} stale"
    )
    return {"created": created, "updated": updated, "skipped": skipped, "missing": missing}


def probe_ip(vm: VirtualMachine, log, *, allow_scan: bool = True) -> str | None:
    """探测单台虚拟机的 IP 并回填。"""
    if not vm.exists:
        log.warn("vmx not found, skipping")
        return None
    if not vmrun.is_running(vm.vmx_path):
        log.info("VM is not running, skipping")
        return None

    cidr = ""
    if vm.template and vm.template.gateway:
        cidr = network.subnet_of(vm.template.gateway, vm.template.prefix_len)
    elif vm.ip_address:
        cidr = network.subnet_of(vm.ip_address, 24)

    # 先校正 MAC：探测的第 3 级兜底就是按 MAC 匹配 ARP，MAC 错了整条链路都断
    mac = vm.mac_address
    try:
        live_mac = vmx.mac_address(vm.vmx_path)
        if live_mac and network.normalize_mac(live_mac) != network.normalize_mac(mac):
            log.info(f"{vm.name}: MAC changed {mac} -> {live_mac}")
            mac = live_mac
            VirtualMachine.objects.filter(pk=vm.pk).update(mac_address=mac)
    except (vmx.VmxError, OSError):
        pass

    found, how = network.detect_ip(
        vm.vmx_path,
        mac=mac,
        cidr=cidr,
        allow_scan=allow_scan,
    )
    if not found:
        log.warn(f"{vm.name}: IP probe failed (Tools not ready and no matching MAC in ARP)")
        return None

    if vm.ip_address == found:
        log.info(f"{vm.name}: {found} ({how})")
    else:
        log.ok(
            f"{vm.name}: {found} ({how})" + (f", previous address {vm.ip_address}" if vm.ip_address else "")
        )
        if vm.ip_address and vm.ip_address != found:
            IpLease.release(vm.ip_address)

    VirtualMachine.objects.filter(pk=vm.pk).update(
        ip_address=found,
        ip_checked_at=timezone.now(),
    )
    IpLease.reserve(found, vm=vm, mac=mac, kind=LeaseKind.DHCP, note="自动探测")
    return found


def probe_ips(log, *, only_missing: bool = True, allow_scan: bool = True) -> dict:
    """批量探测。默认只补空缺的，避免每次刷新都扫网段。"""
    queryset = VirtualMachine.objects.filter(power_state=PowerState.RUNNING)
    if only_missing:
        queryset = queryset.filter(ip_address__isnull=True)

    found = 0
    for vm in queryset:
        if probe_ip(vm, log, allow_scan=allow_scan):
            found += 1

    total = queryset.count()
    log.ok(f"Probe completed: {found}/{total} VMs got an address")
    return {"checked": total, "found": found}


def refresh_all(log) -> dict:
    """状态 + IP + 快照的完整巡检，适合挂到定时任务上。"""
    changed = power.refresh_states(log)
    log.info(f"{changed} VMs changed power state")

    ip_result = probe_ips(log, only_missing=True)

    snap_total = 0
    for vm in VirtualMachine.objects.filter(power_state__in=[PowerState.RUNNING, PowerState.STOPPED]):
        if not vm.exists:
            continue
        try:
            result = snapshot.sync_snapshots(vm)
            snap_total += result["created"]
        except Exception as exc:  # 单台失败不影响整体
            log.warn(f"{vm.name}: snapshot sync failed: {exc}")
    if snap_total:
        log.info(f"Added {snap_total} snapshot records")

    return {"power_changed": changed, **ip_result, "snapshots": snap_total}
