"""创建虚拟机的编排流程。

一条完整的创建链路：

    预检 → 占 IP → 克隆 → 改写 vmx → 开机 → 等 Guest 就绪
         → 设主机名 → 配静态 IP → 校验连通 → 下发 SSH 公钥

设计取舍：

* **失败保留现场**。任何一步失败都不自动删除已经克隆出来的虚拟机 —— 那是最
  有价值的排查证据。Job 里会记录卡在哪一步，用户可以重试「配置」或手动删除。
* **每一步都能单独重跑**。``reconfigure`` 复用同一套函数，只是跳过克隆。
* 所有耗时步骤都通过 ``log`` 回调上报，前端能看到实时进度。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from vmmanager.models import (
    CloneType,
    IpLease,
    LeaseKind,
    OsFamily,
    PowerState,
    VirtualMachine,
    VmTemplate,
    hostname_for,
    vm_name_validator,
)

from . import guest, network, vmrun, vmx, vnc
from .vmrun import VmrunError


class ProvisionError(RuntimeError):
    """创建流程失败。``step`` 标记卡住的阶段，方便前端提示。"""

    def __init__(self, message: str, *, step: str = ""):
        super().__init__(message)
        self.step = step


@dataclass
class ProvisionSpec:
    """创建一台虚拟机所需的全部输入。"""

    name: str
    template: VmTemplate
    cpus: int
    memory_mb: int
    ip: str = ""
    gateway: str = ""
    dns: str = ""
    clone_type: str = CloneType.LINKED
    guest_password: str = ""
    hostname: str = ""
    deploy_ssh_key: bool = True
    notes: str = ""
    owner: object = None
    flavor: object = None
    extra_vmx: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------
def preflight(spec: ProvisionSpec) -> list[str]:
    """返回「不能创建」的原因列表，空列表表示可以开始。"""
    problems: list[str] = []

    try:
        vm_name_validator(spec.name)
    except Exception as exc:  # ValidationError
        problems.append(f"Invalid name: {exc.messages[0] if hasattr(exc, 'messages') else exc}")

    if VirtualMachine.objects.filter(name__iexact=spec.name).exists():
        problems.append(f"A VM with the same name already exists: {spec.name}")

    if not spec.template.exists:
        problems.append(f"Template vmx not found: {spec.template.vmx_path}")

    if spec.template.snapshot:
        try:
            names = vmrun.list_snapshots(spec.template.vmx_path)
        except VmrunError as exc:
            problems.append(f"Failed to list template snapshots: {exc}")
            names = []
        if names and spec.template.snapshot not in names:
            problems.append(
                f"Snapshot {spec.template.snapshot} not found in template (existing: {', '.join(names) or 'none'})"
            )

    if spec.cpus < 1:
        problems.append("vCPU must be at least 1")
    if spec.memory_mb < 256:
        problems.append("Memory must be at least 256 MB")

    # 主机内存是否够（粗略，不阻断只提示）
    host_mem = _host_memory_mb()
    if host_mem and spec.memory_mb > host_mem * 0.75:
        problems.append(
            f"Requested memory {spec.memory_mb} MB exceeds 75% of host physical memory ({host_mem} MB)"
        )

    target_vmx = target_vmx_path(spec.name)
    if os.path.exists(target_vmx):
        problems.append(f"Target vmx already exists: {target_vmx}")

    return problems


def _host_memory_mb() -> int:
    """主机物理内存，拿不到返回 0。

    走 ``metrics.host_metrics()`` 而不是自己调 ``wmic`` —— 后者在
    Windows 11 24H2 里已经被移除了。
    """
    try:
        from . import metrics

        return int(metrics.host_metrics().get("memory_total_mb") or 0)
    except Exception:
        return 0


def target_vmx_path(name: str) -> str:
    base = settings.VMM["BASE_DIR"]
    return os.path.join(base, name, f"{name}.vmx")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def provision(spec: ProvisionSpec, log) -> VirtualMachine:
    """执行创建。``log`` 需提供 info/ok/warn/error/progress 方法。"""
    problems = preflight(spec)
    if problems:
        raise ProvisionError("Preflight failed: " + "; ".join(problems), step="preflight")

    template = spec.template
    target = target_vmx_path(spec.name)
    guest_password = spec.guest_password or template.guest_password
    host = spec.hostname or hostname_for(spec.name)

    # -- 1. 占 IP ---------------------------------------------------------
    ip = spec.ip.strip()
    lease = None
    if ip:
        lease = IpLease.reserve(ip, kind=LeaseKind.STATIC, note=spec.name)
        if lease is None:
            raise ProvisionError(
                f"IP {ip} is already reserved by another VM; pick another or leave blank for auto-assignment",
                step="reserve-ip",
            )
        log.info(f"IP {ip} reserved")
    else:
        cidr = network.subnet_of(template.gateway, template.prefix_len) if template.gateway else ""
        if cidr:
            free = network.next_free_ip(
                cidr,
                reserved=set(IpLease.objects.values_list("ip", flat=True)),
            )
            if free:
                ip = free
                lease = IpLease.reserve(ip, kind=LeaseKind.STATIC, note=spec.name)
                log.info(f"Auto-assigned IP {ip}")
            else:
                log.warn("No free IP in subnet, skipping static IP configuration")
        else:
            log.info("No gateway specified, skipping automatic IP assignment")

    try:
        vm = _do_provision(spec, log, target, host, ip, guest_password, lease)
    except ProvisionError:
        # 保留现场：租约回滚，但虚拟机文件不删
        if lease is not None and ip:
            IpLease.release(ip)
        raise
    except Exception as exc:
        if lease is not None and ip:
            IpLease.release(ip)
        raise ProvisionError(f"Unexpected error during provisioning: {exc}", step="unknown") from exc
    return vm


def _do_provision(spec, log, target: str, host: str, ip: str, guest_password: str, lease):
    template = spec.template
    name = spec.name

    # 先建数据库记录，后续步骤可以持续更新状态
    vm = VirtualMachine.objects.create(
        name=name,
        hostname=host,
        vmx_path=target,
        template=template,
        flavor=spec.flavor,
        owner=spec.owner if getattr(spec.owner, "pk", None) else None,
        cpus=spec.cpus,
        memory_mb=spec.memory_mb,
        clone_type=spec.clone_type,
        ip_address=ip or None,
        power_state=PowerState.UNKNOWN,
        notes=spec.notes,
    )
    if lease is not None:
        lease.vm = vm
        lease.save(update_fields=["vm", "updated_at"])

    # -- 2. 克隆 ----------------------------------------------------------
    linked = spec.clone_type != CloneType.FULL
    log.progress(5)

    # 模板运行中也能克隆（实测通过），但基础盘会偏离快照，克隆结果的一致性没保证。
    # 这里只警告不阻断：有些模板平时就是开着维护的，硬拦反而挡住用户。
    try:
        if vmrun.is_running(template.vmx_path):
            log.warn(
                "Template is running; continuing clone anyway. For snapshot-consistent clones, power it off first"
            )
    except VmrunError:
        pass

    log.info(
        f"Cloning template {template.name} ({'linked clone' if linked else 'full clone'}"
        f"{' from snapshot ' + template.snapshot if template.snapshot else ''})"
    )
    try:
        vmrun.clone(
            template.vmx_path,
            target,
            snapshot=template.snapshot,
            clone_name=name,
            linked=linked,
        )
    except VmrunError as exc:
        raise ProvisionError(f"Clone failed: {exc}", step="clone") from exc

    if not os.path.isfile(target):
        raise ProvisionError(f"Clone reported success but vmx not found: {target}", step="clone")
    log.ok(f"Clone completed -> {target}")
    log.progress(20)

    # -- 3. 写规格 --------------------------------------------------------
    log.info(f"Applying spec {spec.cpus} vCPU / {spec.memory_mb} MB")
    try:
        updates = {
            "displayName": name,
            "memSize": spec.memory_mb,
            "numvcpus": spec.cpus,
            # 拓扑要跟着 vCPU 数一起算：模板里留着 cpuid.coresPerSocket = 2，
            # 直接把 numvcpus 写成奇数就会让「核数 × 路数」对不上。
            "cpuid.coresPerSocket": vmx.cpu_topology(spec.cpus),
            **spec.extra_vmx,
        }
        vmx.patch(target, updates)
    except (vmx.VmxError, OSError) as exc:
        raise ProvisionError(f"Failed to write vmx: {exc}", step="configure") from exc
    log.ok("Spec applied")
    log.progress(30)

    # -- 3.5 VNC 配置（关机状态下写，开机后即有网页控制台） ------------------
    try:
        vnc.ensure_config(vm, log=log.info)
    except Exception as exc:  # noqa: BLE001 — 控制台是锦上添花，别挡创建
        log.warn(f"VNC config skipped: {exc}")

    # -- 4. 开机 ----------------------------------------------------------
    log.info("Powering on VM (nogui)")
    try:
        vmrun.start(target, gui=False)
        vmrun.wait_power_state(target, "running", timeout=120)
    except VmrunError as exc:
        raise ProvisionError(f"Power-on failed: {exc}", step="power-on") from exc
    VirtualMachine.objects.filter(pk=vm.pk).update(
        power_state=PowerState.RUNNING,
        state_checked_at=timezone.now(),
    )
    vm.power_state = PowerState.RUNNING
    log.ok("VM powered on")
    log.progress(40)

    # -- 4.5 回读 MAC -----------------------------------------------------
    # 必须在**开机之后**才读：克隆出来的 vmx 还带着模板快照里的
    # ethernet0.generatedAddress，而 VMware 在首次开机、发现 uuid.bios 为空时
    # 会重新生成 UUID 和 MAC。开机前读到的地址从开机那一刻起就是废的，
    # 拿它去匹配 ARP 永远找不到对应的 IP。
    try:
        mac = vmx.mac_address(target)
        if mac:
            VirtualMachine.objects.filter(pk=vm.pk).update(mac_address=mac)
            vm.mac_address = mac
            if lease is not None:
                lease.mac = network.normalize_mac(mac)
                lease.save(update_fields=["mac", "updated_at"])
            log.ok(f"MAC {mac}")
    except (vmx.VmxError, OSError) as exc:
        log.warn(f"Failed to read MAC (does not affect power-on): {exc}")

    # -- 5. 等 Guest ------------------------------------------------------
    if template.os_family != OsFamily.LINUX:
        log.warn("Non-Linux guest, skipping hostname / IP / SSH public key configuration")
        log.progress(100)
        return vm

    session = guest.GuestSession(target, template.guest_user, guest_password)
    tools_wait = settings.VMM["TOOLS_WAIT"]
    log.info(f"Waiting for guest ready (up to {tools_wait}s)")
    ready = session.wait_ready(
        timeout=tools_wait,
        on_tick=lambda sec: log.info(f"  waited {sec}s, guest not ready yet..."),
    )
    if not ready:
        log.warn(f"Guest still not ready after {tools_wait}s, skipping remaining configuration")
        log.warn(
            "VM is powered on; you can click 'Reconfigure' on the detail page later, or check open-vm-tools in the template"
        )
        if ip:
            VirtualMachine.objects.filter(pk=vm.pk).update(ip_address=None)
            vm.ip_address = None
            IpLease.release(ip)
            log.warn(f"Released {ip} in the database to avoid showing a wrong address")
        log.progress(100)
        return vm
    log.ok("Guest ready")
    log.progress(55)

    # -- 6. 主机名 --------------------------------------------------------
    log.info(f"Setting hostname {host}")
    try:
        session.set_hostname(host)
        VirtualMachine.objects.filter(pk=vm.pk).update(hostname=host)
        log.ok("Hostname set")
    except guest.GuestError as exc:
        log.warn(f"Failed to set hostname (does not affect power-on): {exc}")
    log.progress(65)

    # -- 7. 静态 IP -------------------------------------------------------
    detected_ip = None
    if ip:
        log.info(
            f"Configuring static IP {ip}/{template.prefix_len} (connection {template.guest_connection or 'auto-detect'})"
        )
        try:
            session.set_static_ip(
                ip,
                prefix=template.prefix_len,
                gateway=spec.gateway or template.gateway,
                connection=template.guest_connection,
                dns=spec.dns or template.dns,
            )
        except guest.GuestError as exc:
            # 切换 IP 时连接会断，命令返回异常是常见现象，继续用 ping 校验
            log.warn(f"nmcli returned an error (common when switching IP, continuing verification): {exc}")

        if network.wait_port(ip, 22, timeout=60) or network.ping(ip):
            detected_ip = ip
            log.ok(f"{ip} is reachable")
        else:
            # 客户机可能自己拿到了别的地址，退回探测
            found, how = network.detect_ip(
                target,
                mac=vm.mac_address,
                cidr=network.subnet_of(ip, template.prefix_len),
            )
            if found and found != ip:
                log.warn(f"{ip} not reachable yet, but guest actual address is {found} ({how})")
                detected_ip = found
            else:
                log.warn(f"{ip} not reachable yet, guest network may still be restarting")
                detected_ip = ip
    else:
        found, how = network.detect_ip(
            target,
            mac=vm.mac_address,
            cidr=network.subnet_of(template.gateway, template.prefix_len) if template.gateway else "",
        )
        if found:
            detected_ip = found
            log.ok(f"Detected guest address {found} ({how})")
    log.progress(85)

    # -- 8. SSH 公钥 ------------------------------------------------------
    if spec.deploy_ssh_key and template.deploy_ssh_key:
        log.info("Deploying local SSH public key")
        pubkey = guest.find_public_key()
        if pubkey:
            try:
                session.install_ssh_key(pubkey)
                log.ok("SSH public key written to authorized_keys")
            except guest.GuestError as exc:
                log.warn(f"Failed to deploy SSH public key: {exc}")
        else:
            log.warn("No usable local SSH public key (is ssh-keygen available?), skipping")

    # -- 9. 收尾 ----------------------------------------------------------
    updates = {}
    if detected_ip:
        # 释放旧租约，登记真实地址
        if ip and ip != detected_ip:
            IpLease.release(ip)
        IpLease.reserve(
            detected_ip,
            vm=vm,
            mac=vm.mac_address,
            kind=LeaseKind.STATIC,
            note=name,
        )
        updates["ip_address"] = detected_ip
        updates["ip_checked_at"] = timezone.now()
    updates["power_state"] = PowerState.RUNNING
    updates["state_checked_at"] = timezone.now()
    VirtualMachine.objects.filter(pk=vm.pk).update(**updates)

    vm.refresh_from_db()
    log.progress(100)
    log.ok(f"Provisioning completed: {vm.name} -> {vm.ip_address or 'no IP detected'}")
    return vm


# ---------------------------------------------------------------------------
# 重试：只补配置，不重新克隆
# ---------------------------------------------------------------------------
def reconfigure_guest(vm: VirtualMachine, log, *, guest_password: str = "") -> None:
    """对一台已开机的虚拟机重跑「主机名 + IP + 公钥」。"""
    template = vm.template
    if template is None:
        raise ProvisionError(
            "VM has no associated template; cannot determine guest credentials", step="preflight"
        )

    password = guest_password or template.guest_password
    session = guest.GuestSession(vm.vmx_path, template.guest_user, password)

    if not vmrun.is_running(vm.vmx_path):
        log.info("VM is not running, powering on first")
        vmrun.start(vm.vmx_path, gui=False)
        vmrun.wait_power_state(vm.vmx_path, "running")

    log.info("Waiting for guest ready")
    if not session.wait_ready(
        timeout=settings.VMM["TOOLS_WAIT"], on_tick=lambda s: log.info(f"  waited {s}s...")
    ):
        raise ProvisionError("Guest still not ready; check the open-vm-tools status", step="wait-guest")
    log.ok("Guest ready")

    host = vm.hostname or hostname_for(vm.name)
    log.info(f"Setting hostname {host}")
    session.set_hostname(host)

    if vm.ip_address:
        log.info(f"Configuring static IP {vm.ip_address}")
        session.set_static_ip(
            vm.ip_address,
            prefix=template.prefix_len,
            gateway=template.gateway,
            connection=template.guest_connection,
            dns=template.dns,
        )
        if network.wait_port(vm.ip_address, 22, timeout=60):
            log.ok(f"{vm.ip_address} is reachable")
        else:
            log.warn(f"{vm.ip_address} is still unreachable")

    if template.deploy_ssh_key:
        pubkey = guest.find_public_key()
        if pubkey:
            try:
                session.install_ssh_key(pubkey)
                log.ok("SSH public key deployed")
            except guest.GuestError as exc:
                log.warn(f"Failed to deploy SSH public key: {exc}")

    VirtualMachine.objects.filter(pk=vm.pk).update(
        ip_checked_at=timezone.now(),
        state_checked_at=timezone.now(),
    )
    log.ok("Configuration completed")
