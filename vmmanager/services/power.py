"""电源与删除操作。

原则：**先确认状态，再动命令**。``vmrun`` 在状态不对时返回的错误信息很难懂
（比如对着没开机的机器 ``stop``），所以每一步都先自己判断一次。
"""

from __future__ import annotations

import os
import shutil
import time

from django.conf import settings
from django.utils import timezone

from vmmanager.models import IpLease, PowerState, VirtualMachine

from . import vmrun
from .vmrun import VmrunError


class PowerError(RuntimeError):
    pass


def _refresh(vm: VirtualMachine) -> None:
    running = vmrun.is_running(vm.vmx_path)
    state = PowerState.RUNNING if running else PowerState.STOPPED
    vm.mark_state(state)


def power_on(vm: VirtualMachine, log, *, gui: bool = False) -> None:
    if not vm.exists:
        raise PowerError(f"vmx not found: {vm.vmx_path}")
    if vmrun.is_running(vm.vmx_path):
        log.warn("VM is already running")
        vm.mark_state(PowerState.RUNNING)
        return

    log.info(f"Powering on {vm.name}")
    try:
        vmrun.start(vm.vmx_path, gui=gui)
    except VmrunError as exc:
        raise PowerError(f"Power-on failed: {exc}") from exc

    if not vmrun.wait_power_state(vm.vmx_path, "running", timeout=120):
        log.warn("Power-on command issued but state not confirmed yet; refresh to check")
    vm.mark_state(PowerState.RUNNING)
    log.ok("VM powered on")


def power_off(vm: VirtualMachine, log, *, force: bool = False) -> None:
    if not vmrun.is_running(vm.vmx_path):
        log.warn("VM is already powered off")
        vm.mark_state(PowerState.STOPPED)
        return

    if force:
        log.info("Forcing power off")
        try:
            vmrun.stop(vm.vmx_path, hard=True)
        except VmrunError as exc:
            raise PowerError(f"Hard power-off failed: {exc}") from exc
    else:
        log.info("Requesting guest graceful shutdown (requires VMware Tools)")
        try:
            vmrun.stop(vm.vmx_path, hard=False)
        except VmrunError as exc:
            # stop(hard=False) 内部已经尝试过退回 hard，到这里说明两条路都失败了
            raise PowerError(f"Power-off failed: {exc}") from exc

    if not vmrun.wait_power_state(vm.vmx_path, "stopped", timeout=180):
        log.warn("Timed out waiting for power-off; the VM may still be shutting down, refresh later")
    vm.mark_state(PowerState.STOPPED)
    log.ok("VM powered off")


def reset(vm: VirtualMachine, log) -> None:
    if not vmrun.is_running(vm.vmx_path):
        raise PowerError("VM is not running; cannot restart")
    log.info("Restarting VM")
    try:
        vmrun.reset(vm.vmx_path)
    except VmrunError as exc:
        raise PowerError(f"Restart failed: {exc}") from exc
    vm.mark_state(PowerState.RUNNING)
    log.ok("Restart command issued")


def suspend(vm: VirtualMachine, log) -> None:
    if not vmrun.is_running(vm.vmx_path):
        raise PowerError("VM is not running; cannot suspend")
    log.info("Suspending VM")
    try:
        vmrun.suspend(vm.vmx_path)
    except VmrunError as exc:
        raise PowerError(f"Suspend failed: {exc}") from exc
    vm.mark_state(PowerState.SUSPENDED)
    log.ok("VM suspended")


def delete_vm(vm: VirtualMachine, log, *, remove_files: bool = True) -> None:
    """删除虚拟机。先关机，再交给 vmrun 删，最后兜底清理目录。"""
    name = vm.name
    vmx_path = vm.vmx_path
    directory = os.path.dirname(vmx_path)
    base_dir = os.path.normcase(os.path.abspath(settings.VMM["BASE_DIR"]))

    if remove_files and directory and os.path.normcase(os.path.abspath(directory)) == base_dir:
        # 极端情况：vmx 直接放在根目录下，删目录会连累所有虚拟机
        raise PowerError(
            "Refusing to delete: VM directory equals the root directory; please confirm manually"
        )

    if vmrun.is_running(vmx_path):
        log.info("VM is still running, powering off first")
        try:
            vmrun.stop(vmx_path, hard=False)
        except VmrunError:
            log.warn("Graceful shutdown failed, falling back to hard power-off")
            vmrun.stop(vmx_path, hard=True)
        vmrun.wait_power_state(vmx_path, "stopped", timeout=120)

    if remove_files:
        if os.path.isfile(vmx_path):
            log.info("Calling vmrun deleteVM")
            try:
                vmrun.delete(vmx_path)
            except VmrunError as exc:
                log.warn(f"deleteVM failed (common for linked clones), falling back to manual cleanup: {exc}")

        if os.path.isdir(directory) and os.path.isfile(os.path.join(directory, f"{name}.vmx")):
            log.warn("Directory still exists, performing fallback deletion")
            shutil.rmtree(directory, ignore_errors=True)

        # 再检查一次
        deadline = time.time() + 20
        while time.time() < deadline and os.path.isdir(directory):
            time.sleep(0.5)
        if os.path.isdir(directory):
            remaining = []
            for _root, _dirs, files in os.walk(directory):
                remaining.extend(files)
                if len(remaining) > 5:
                    break
            raise PowerError(
                f"Directory not fully deleted: {directory} ({len(remaining)} files remaining, "
                "possibly held by VMware or antivirus software)"
            )
        log.ok("Disk files deleted")
    else:
        log.info("Keeping disk files per settings; only the record was deleted")

    if vm.ip_address:
        IpLease.release(vm.ip_address)
        log.info(f"Released IP lease {vm.ip_address}")

    vm.delete()
    log.ok(f"Deleted {name}")


def refresh_states(log=None) -> int:
    """刷新所有虚拟机的电源状态，返回发生变化的数量。"""
    try:
        running = vmrun.list_running_set()
    except VmrunError as exc:
        if log:
            log.warn(f"Failed to read running list: {exc}")
        return 0

    changed = 0
    now = timezone.now()
    for vm in VirtualMachine.objects.all():
        state = PowerState.RUNNING if vmrun.normpath(vm.vmx_path) in running else PowerState.STOPPED
        if not vm.exists:
            state = PowerState.UNKNOWN
        if vm.power_state != state:
            changed += 1
            if log:
                log.info(f"{vm.name}: {vm.get_power_state_display()} -> {state}")
        VirtualMachine.objects.filter(pk=vm.pk).update(
            power_state=state,
            state_checked_at=now,
        )
    return changed
