"""快照管理。

``vmrun listSnapshots`` 只返回名字，没有层级、时间和大小。这里的策略是：

* 名字以 vmrun 为准（唯一的真实来源）；
* 说明、创建人等附加信息记在自己的 ``Snapshot`` 表里，按名字对齐；
* 对账时**不删除** vmrun 已经不存在的记录，只在页面上标为「已失效」，
  避免误删用户手工记录的信息。
"""

from __future__ import annotations

from django.utils import timezone

from vmmanager.models import PowerState, Snapshot, VirtualMachine

from . import vmrun
from .vmrun import VmrunError


class SnapshotError(RuntimeError):
    pass


def list_remote(vm: VirtualMachine) -> list[str]:
    try:
        return vmrun.list_snapshots(vm.vmx_path)
    except VmrunError:
        return []


def sync_snapshots(vm: VirtualMachine) -> dict:
    """把 vmrun 的快照列表对齐到数据库。"""
    try:
        remote = set(vmrun.list_snapshots(vm.vmx_path))
    except VmrunError:
        remote = set()

    local = {snap.name: snap for snap in vm.snapshots.all()}
    created = 0
    removed = 0

    for name in remote:
        if name not in local:
            Snapshot.objects.create(vm=vm, name=name)
            created += 1

    for name, snap in local.items():
        if name not in remote:
            # 只做标记，不物理删除
            if snap.description != "[stale]":
                snap.description = f"[stale] {snap.description}".strip()
                snap.save(update_fields=["description", "updated_at"])
            removed += 1

    return {"remote": len(remote), "created": created, "stale": removed}


def create(vm: VirtualMachine, name: str, log, *, description: str = "", user=None) -> Snapshot:
    name = (name or "").strip()
    if not name:
        raise SnapshotError("Snapshot name cannot be empty")
    if vm.snapshots.filter(name=name).exists():
        raise SnapshotError(f"Snapshot with the same name already exists: {name}")

    log.info(f"Creating snapshot {name} for {vm.name}")
    try:
        vmrun.create_snapshot(vm.vmx_path, name)
    except VmrunError as exc:
        raise SnapshotError(f"Snapshot creation failed: {exc}") from exc

    snap = Snapshot.objects.create(
        vm=vm,
        name=name,
        description=description,
        created_by=user if getattr(user, "pk", None) else None,
    )
    log.ok("Snapshot created")
    return snap


def revert(vm: VirtualMachine, name: str, log) -> None:
    if not vm.snapshots.filter(name=name).exists() and name not in list_remote(vm):
        raise SnapshotError(f"Snapshot not found: {name}")

    log.info(f"Reverting {vm.name} to snapshot {name}")
    try:
        vmrun.revert_snapshot(vm.vmx_path, name)
    except VmrunError as exc:
        raise SnapshotError(f"Revert failed: {exc}") from exc

    Snapshot.objects.filter(vm=vm).update(is_current=False)
    Snapshot.objects.filter(vm=vm, name=name).update(is_current=True, updated_at=timezone.now())

    # 回滚后电源状态由 VMware 决定（快照可能记录的是开机状态）
    running = vmrun.is_running(vm.vmx_path)
    vm.mark_state(PowerState.RUNNING if running else PowerState.STOPPED)
    log.ok("Reverted")


def delete(vm: VirtualMachine, name: str, log) -> None:
    log.info(f"Deleting snapshot {name}")
    try:
        vmrun.delete_snapshot(vm.vmx_path, name)
    except VmrunError as exc:
        raise SnapshotError(f"Snapshot deletion failed: {exc}") from exc
    Snapshot.objects.filter(vm=vm, name=name).delete()
    log.ok("Snapshot deleted")
