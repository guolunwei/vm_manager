"""克隆一台已有虚拟机。

与「从模板创建」的区别：

* 源不是模板而是 ``VirtualMachine``——克隆出来的机器带着源机的磁盘内容
  （装过的软件、改过的配置都在），所以它天然没有「初始化」这一步；
* **链接克隆不可行**：源的磁盘本来就是它模板的 overlay，在 overlay 上再叠
  一层属于 VMware 不支持的操作。所以这里只做**完整克隆**；
* 源机在跑也能克隆，但必须有快照（vmrun 的要求：克隆基于快照的磁盘状态）。
  没有快照就明确报错，让用户先打快照或关机；
* 初始化只做 Guest 侧的「改名 + 配 IP + 公钥」（复用 ``provision`` 的
  ``reconfigure_guest``），密码沿用源机的模板配置。

失败保留现场：克隆出来的文件不自动删（与创建同一哲学）。
"""

from __future__ import annotations

import os

from django.utils import timezone

from vmmanager.models import (
    CloneType,
    IpLease,
    LeaseKind,
    OsFamily,
    PowerState,
    VirtualMachine,
    vm_name_validator,
)

from . import power, provision, vmrun, vmx, vnc
from .vmrun import VmrunError


class CloneError(RuntimeError):
    """克隆失败。"""


def preflight(source: VirtualMachine, name: str, ip: str) -> list[str]:
    """返回不能克隆的原因列表。"""
    problems: list[str] = []

    try:
        vm_name_validator(name)
    except Exception as exc:
        problems.append(f"Invalid name: {exc.messages[0] if hasattr(exc, 'messages') else exc}")

    if name.lower() == source.name.lower():
        problems.append("New name must differ from the source VM")
    if VirtualMachine.objects.filter(name__iexact=name).exists():
        problems.append(f"A VM with the same name already exists: {name}")

    if not source.exists:
        problems.append(f"Source vmx not found: {source.vmx_path}")

    if os.path.exists(provision.target_vmx_path(name)):
        problems.append(f"Target vmx already exists: {provision.target_vmx_path(name)}")

    if ip:
        if IpLease.objects.filter(ip=ip).exists():
            problems.append(f"IP {ip} is already reserved")

    try:
        running = vmrun.is_running(source.vmx_path)
    except VmrunError:
        running = False
    if running and source.clone_type == CloneType.LINKED:
        problems.append(
            "Source is a linked clone and is running; vmrun cannot clone it in this state. Power it off first"
        )

    return problems


def clone_vm(
    source: VirtualMachine,
    name: str,
    log,
    *,
    ip: str = "",
    cpus: int = 0,
    memory_mb: int = 0,
    notes: str = "",
    owner=None,
) -> VirtualMachine:
    """完整克隆一台已有虚拟机。``log`` 需提供 info/ok/warn/error/progress。"""
    problems = preflight(source, name, ip)
    if problems:
        raise CloneError("Preflight failed: " + "; ".join(problems))

    template = source.template
    target = provision.target_vmx_path(name)
    from vmmanager.models import hostname_for

    host = hostname_for(name)

    # -- 占 IP -------------------------------------------------------------
    lease = None
    if ip:
        lease = IpLease.reserve(ip, kind=LeaseKind.STATIC, note=name)
        if lease is None:
            raise CloneError(f"IP {ip} is already reserved")
        log.info(f"IP {ip} reserved")

    try:
        return _do_clone(source, name, host, target, template, lease, ip, cpus, memory_mb, notes, owner, log)
    except Exception:
        if lease is not None and ip:
            IpLease.release(ip)
        raise


def _do_clone(source, name, host, target, template, lease, ip, cpus, memory_mb, notes, owner, log):
    # -- 1. 建记录 ----------------------------------------------------------
    vm = VirtualMachine.objects.create(
        name=name,
        hostname=host,
        vmx_path=target,
        template=template,
        flavor=source.flavor,
        owner=owner if getattr(owner, "pk", None) else None,
        cpus=cpus or source.cpus,
        memory_mb=memory_mb or source.memory_mb,
        clone_type=CloneType.FULL,
        ip_address=ip or None,
        power_state=PowerState.UNKNOWN,
        notes=notes or f"Cloned from {source.name}",
    )
    if lease is not None:
        lease.vm = vm
        lease.save(update_fields=["vm", "updated_at"])

    # -- 2. 克隆 ------------------------------------------------------------
    log.progress(5)
    log.info(f"Full cloning {source.name} -> {name}")
    try:
        vmrun.clone(
            source.vmx_path,
            target,
            snapshot="",  # 无快照时克隆当前磁盘状态；源机在跑且有快照时 vmrun 自行选基础
            clone_name=name,
            linked=False,
        )
    except VmrunError as exc:
        raise CloneError(f"Clone failed: {exc}") from exc

    if not os.path.isfile(target):
        raise CloneError(f"Clone reported success but vmx not found: {target}")
    log.ok(f"Clone completed -> {target}")
    log.progress(40)

    # -- 3. 写规格（仅覆盖时） ----------------------------------------------
    updates: dict = {"displayName": name}
    if cpus:
        updates["numvcpus"] = cpus
        updates["cpuid.coresPerSocket"] = vmx.cpu_topology(cpus)
    if memory_mb:
        updates["memSize"] = memory_mb
    try:
        vmx.patch(target, updates)
    except (vmx.VmxError, OSError) as exc:
        log.warn(f"Failed to write vmx spec (clone keeps source spec): {exc}")

    # -- 4. VNC -------------------------------------------------------------
    try:
        vnc.ensure_config(vm, log=log.info)
    except Exception as exc:  # noqa: BLE001
        log.warn(f"VNC config skipped: {exc}")

    # -- 5. Guest 初始化 ----------------------------------------------------
    if template is None or template.os_family != OsFamily.LINUX:
        log.warn("No Linux template associated; skipping hostname / IP / key configuration")
        VirtualMachine.objects.filter(pk=vm.pk).update(
            power_state=PowerState.STOPPED, state_checked_at=timezone.now()
        )
        log.progress(100)
        log.ok(f"Cloned {name} (powered off)")
        return vm

    log.info("Powering on to initialize guest (hostname / IP / SSH key)")
    try:
        power.power_on(vm, log)
    except Exception as exc:  # noqa: BLE001 — 开机失败不算克隆失败
        log.warn(f"Power-on failed after clone: {exc}")
        log.progress(100)
        return vm

    try:
        provision.reconfigure_guest(vm, log)
    except provision.ProvisionError as exc:
        log.warn(f"Guest initialization failed: {exc}")
        log.warn("Disk content is fine; use 'Reconfigure' on the detail page to retry")
    log.progress(100)
    return vm
