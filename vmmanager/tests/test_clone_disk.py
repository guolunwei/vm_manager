"""克隆虚拟机与磁盘扩容的回归测试。

约定提醒：

* 克隆只做完整克隆（链接克隆再叠 overlay 是 VMware 不支持的）；
* 扩容只做完整克隆的磁盘（链接克隆的磁盘是模板基础盘的 overlay）；
* vmdk 容量从描述符的 ``RW <sectors>`` 行解析，失败时退回文件大小下界。
"""

from __future__ import annotations

import subprocess

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from vmmanager.jobs import handlers
from vmmanager.jobs.base import NullContext
from vmmanager.models import CloneType, Job, JobKind, VirtualMachine
from vmmanager.services import clone, disk

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_superuser(
        username="root-user", email="", password="pw-clone-admin"
    )


@pytest.fixture
def client(admin):
    c = Client()
    c.force_login(admin)
    return c


# ---------------------------------------------------------------------------
# vmdk 容量解析
# ---------------------------------------------------------------------------
def _make_vm_with_disk(tmp_path, name="disk-vm"):
    """一台 vmx + 20GB 磁盘描述符都在磁盘上的机器。"""
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.vmx").write_text(
        'displayName = "x"\r\nscsi0:0.fileName = "disk.vmdk"\r\n', encoding="utf-8"
    )
    (directory / "disk.vmdk").write_text(
        "# Disk DescriptorFile\n"
        "version=1\n"
        'RW 41922560 FLAT "disk-flat.vmdk" 0\n'
        'ddb.geometry.cylinders = "2610"\n',
        encoding="ascii",
    )
    (directory / "disk-flat.vmdk").write_bytes(b"\x00" * 1024)
    return VirtualMachine.objects.create(
        name=name,
        vmx_path=str(directory / f"{name}.vmx"),
        clone_type=CloneType.FULL,
        cpus=2,
        memory_mb=2048,
    )


def test_disk_capacity_from_descriptor(tmp_path):
    vm = _make_vm_with_disk(tmp_path)
    # 41922560 扇区 × 512 = 20 GiB
    assert disk.disk_capacity_gb(vm.vmx_path) == 20.0


def test_disk_capacity_falls_back_to_file_size(tmp_path):
    """描述符解析不出来时，用相关文件大小兜底。"""
    directory = tmp_path / "opaque"
    directory.mkdir()
    (directory / "opaque.vmx").write_text('scsi0:0.fileName = "disk.vmdk"\r\n', encoding="utf-8")
    (directory / "disk.vmdk").write_bytes(b"\x00" * 512)  # 不是文本描述符
    (directory / "disk-flat.vmdk").write_bytes(b"\x00" * 5 * 1024**3)

    capacity = disk.disk_capacity_bytes(str(directory / "opaque.vmx"))
    assert capacity == 5 * 1024**3


def test_disk_capacity_no_disks(tmp_path):
    directory = tmp_path / "nodisk"
    directory.mkdir()
    (directory / "nodisk.vmx").write_text('displayName = "x"\r\n', encoding="utf-8")
    assert disk.disk_capacity_bytes(str(directory / "nodisk.vmx")) == 0


# ---------------------------------------------------------------------------
# grow_disk 服务
# ---------------------------------------------------------------------------
def test_grow_disk_runs_vdiskmanager(tmp_path, monkeypatch):
    vm = _make_vm_with_disk(tmp_path)
    commands = []
    monkeypatch.setattr(disk, "vdiskmanager_path", lambda: "fake-vdiskmanager")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: (
            commands.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        ),
    )

    logs = []
    disk.grow_disk(vm.vmx_path, 40, logs.append)

    assert len(commands) == 1
    tool, flag, size, target = commands[0]
    assert flag == "-x" and size == "40GB"
    assert target.endswith("disk.vmdk")
    assert any("40 GB" in line for line in logs)


def test_grow_disk_failure_raises(tmp_path, monkeypatch):
    vm = _make_vm_with_disk(tmp_path)
    monkeypatch.setattr(disk, "vdiskmanager_path", lambda: "fake-vdiskmanager")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not growable"),
    )

    with pytest.raises(disk.DiskError, match="not growable"):
        disk.grow_disk(vm.vmx_path, 40, lambda msg: None)


# ---------------------------------------------------------------------------
# 克隆服务
# ---------------------------------------------------------------------------
def test_clone_preflight_blocks_same_name(tmp_path):
    vm = _make_vm_with_disk(tmp_path)
    problems = clone.preflight(vm, vm.name, "")
    assert any("differ" in p for p in problems)


def test_clone_preflight_blocks_existing_name(tmp_path):
    vm = _make_vm_with_disk(tmp_path)
    problems = clone.preflight(vm, "disk-VM".lower(), "")
    assert any("already exists" in p for p in problems)


def test_clone_full_vm(tmp_path, admin, monkeypatch, settings):
    settings.VMM["BASE_DIR"] = str(tmp_path / "vms")
    (tmp_path / "vms").mkdir()
    vm = _make_vm_with_disk(tmp_path)
    monkeypatch.setattr(clone.vmrun, "is_running", lambda path: False)

    cloned_targets = []

    def fake_clone(src, dst, *, snapshot, clone_name, linked, timeout=900):
        cloned_targets.append((src, dst, linked))
        from pathlib import Path

        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text('displayName = "new"\r\n', encoding="utf-8")

    monkeypatch.setattr(clone.vmrun, "clone", fake_clone)
    monkeypatch.setattr(clone.vmx, "patch", lambda path, updates: None)
    monkeypatch.setattr(clone.vnc, "ensure_config", lambda vm, log=None: None)
    monkeypatch.setattr(clone.provision, "reconfigure_guest", lambda vm, log: None)
    monkeypatch.setattr(clone.power, "power_on", lambda vm, log: None)

    logs = []
    new_vm = clone.clone_vm(
        vm,
        "disk-clone",
        type(
            "L",
            (),
            {
                "info": logs.append,
                "ok": logs.append,
                "warn": logs.append,
                "error": logs.append,
                "progress": lambda self, p: None,
            },
        )(),
        owner=admin,
    )

    assert cloned_targets[0][2] is False  # 完整克隆
    new_vm.refresh_from_db()
    assert new_vm.name == "disk-clone"
    assert new_vm.clone_type == CloneType.FULL
    assert new_vm.notes.startswith("Cloned from")


def test_clone_rejects_taken_ip(tmp_path):
    vm = _make_vm_with_disk(tmp_path)
    from vmmanager.models import IpLease

    IpLease.reserve("192.168.47.99", note="other")
    problems = clone.preflight(vm, "new-vm", "192.168.47.99")
    assert any("192.168.47.99" in p for p in problems)


# ---------------------------------------------------------------------------
# 扩容 handler
# ---------------------------------------------------------------------------
def _job_for(vm, payload):
    return Job.objects.create(
        kind=JobKind.GROW_DISK, payload={"vm_id": vm.pk, **payload}, vm=vm, label=vm.name
    )


def test_grow_disk_handler_computes_target(tmp_path, monkeypatch):
    vm = _make_vm_with_disk(tmp_path)  # 20 GB
    monkeypatch.setattr(handlers.disk, "disk_capacity_gb", lambda path: 20.0)
    monkeypatch.setattr(handlers.disk, "grow_disk", lambda path, size, log: logs.append(size) or None)
    monkeypatch.setattr(handlers.vmrun, "is_running", lambda path: False)
    logs = []

    job = _job_for(vm, {"grow_gb": 10})
    result = handlers.handle_grow_disk(job, NullContext(job))

    assert result["size_gb"] == 30  # ceil(20) + 10
    assert result["fs_grown"] is False  # 无模板，跳过 Guest 扩展


def test_grow_disk_handler_rejects_linked_clone(tmp_path, monkeypatch):
    vm = _make_vm_with_disk(tmp_path)
    vm.clone_type = CloneType.LINKED
    vm.save(update_fields=["clone_type"])

    job = _job_for(vm, {"grow_gb": 10})
    with pytest.raises(RuntimeError, match="Linked-clone"):
        handlers.handle_grow_disk(job, NullContext(job))


def test_grow_disk_handler_rejects_running_vm(tmp_path, monkeypatch):
    vm = _make_vm_with_disk(tmp_path)
    monkeypatch.setattr(handlers.vmrun, "is_running", lambda path: True)

    job = _job_for(vm, {"grow_gb": 10})
    with pytest.raises(RuntimeError, match="powered off"):
        handlers.handle_grow_disk(job, NullContext(job))


def test_grow_disk_handler_rejects_non_growing_size(tmp_path, monkeypatch):
    vm = _make_vm_with_disk(tmp_path)
    monkeypatch.setattr(handlers.disk, "disk_capacity_gb", lambda path: 20.0)
    monkeypatch.setattr(handlers.vmrun, "is_running", lambda path: False)

    job = _job_for(vm, {"size_gb": 10})  # 比现在还小
    with pytest.raises(RuntimeError, match="not larger"):
        handlers.handle_grow_disk(job, NullContext(job))


# ---------------------------------------------------------------------------
# 克隆 handler
# ---------------------------------------------------------------------------
def test_clone_handler_links_job_to_new_vm(tmp_path, admin, monkeypatch, settings):
    settings.VMM["BASE_DIR"] = str(tmp_path / "vms")
    (tmp_path / "vms").mkdir()
    vm = _make_vm_with_disk(tmp_path)
    monkeypatch.setattr(clone.vmrun, "is_running", lambda path: False)

    def fake_clone(src, dst, **kwargs):
        from pathlib import Path

        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text('displayName = "x"\r\n', encoding="utf-8")

    monkeypatch.setattr(clone.vmrun, "clone", fake_clone)
    monkeypatch.setattr(clone.vmx, "patch", lambda path, updates: None)
    monkeypatch.setattr(clone.vnc, "ensure_config", lambda vm, log=None: None)

    job = Job.objects.create(
        kind=JobKind.CLONE_VM,
        payload={"source_vm_id": vm.pk, "name": "disk-clone-2"},
        vm=vm,
        label="clone",
        created_by=admin,
    )
    result = handlers.handle_clone_vm(job, NullContext(job))

    assert result["name"] == "disk-clone-2"
    job.refresh_from_db()
    assert job.vm.name == "disk-clone-2"  # 任务已关联到新机器
    assert result["vm_id"] == job.vm.pk


# ---------------------------------------------------------------------------
# 页面与 API
# ---------------------------------------------------------------------------
def test_clone_view_enqueues(client, admin, tmp_path, settings):
    settings.VMM["BASE_DIR"] = str(tmp_path / "vms")
    (tmp_path / "vms").mkdir()
    vm = _make_vm_with_disk(tmp_path)

    response = client.post(f"/vms/{vm.pk}/clone/", {"name": "disk-copy", "ip": ""})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    job = Job.objects.get(pk=data["job_id"])
    assert job.kind == JobKind.CLONE_VM
    assert job.payload["source_vm_id"] == vm.pk


def test_clone_view_rejects_duplicate_name(client, tmp_path):
    vm = _make_vm_with_disk(tmp_path)
    response = client.post(f"/vms/{vm.pk}/clone/", {"name": "DISK-VM", "ip": ""})
    assert response.status_code == 400


def test_grow_disk_view_validates(client, tmp_path):
    vm = _make_vm_with_disk(tmp_path)
    response = client.post(f"/vms/{vm.pk}/grow-disk/", {"grow_gb": "", "size_gb": ""})
    assert response.status_code == 400

    response = client.post(f"/vms/{vm.pk}/grow-disk/", {"grow_gb": "10", "size_gb": "40"})
    assert response.status_code == 400


def test_grow_disk_view_enqueues(client, tmp_path):
    vm = _make_vm_with_disk(tmp_path)
    response = client.post(f"/vms/{vm.pk}/grow-disk/", {"grow_gb": "10", "size_gb": ""})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert Job.objects.get(pk=response.json()["job_id"]).kind == JobKind.GROW_DISK


def test_api_grow_disk(tmp_path, admin):
    from rest_framework.test import APIClient

    vm = _make_vm_with_disk(tmp_path)
    api = APIClient()
    api.force_authenticate(user=admin)
    response = api.post(f"/api/v1/vms/{vm.pk}/grow-disk/", {"grow_gb": 10}, format="json")
    assert response.status_code == 202
    assert response.json()["kind"] == "grow_disk"
