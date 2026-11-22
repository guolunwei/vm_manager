"""规格调整（resize）的回归测试。

核心约定：VMware 只在开机时读 vmx，所以运行中的机器必须
「关机 → 改 vmx → 开机」；改 vmx 失败时绝不能再开机（保留现场）。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.test import APIClient

from vmmanager.jobs import handlers
from vmmanager.jobs.base import NullContext
from vmmanager.models import Job, JobKind, VirtualMachine

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="resizer", password="pw-resize")


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_superuser(
        username="root-user", email="", password="pw-resize-admin"
    )


@pytest.fixture
def vm(db, tmp_path):
    vmx_file = tmp_path / "resize-1" / "resize-1.vmx"
    vmx_file.parent.mkdir()
    vmx_file.write_text(
        'displayName = "resize-1"\r\nmemSize = "4096"\r\nnumvcpus = "2"\r\n',
        encoding="utf-8",
    )
    return VirtualMachine.objects.create(
        name="resize-1",
        vmx_path=str(vmx_file),
        cpus=2,
        memory_mb=4096,
    )


@pytest.fixture
def job(db, vm):
    return Job.objects.create(
        kind=JobKind.RESIZE,
        payload={"vm_id": vm.pk, "cpus": 4, "memory_mb": 8192},
        vm=vm,
        label=vm.name,
    )


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------
def test_resize_stopped_vm_patches_vmx(job, vm, monkeypatch):
    calls = []
    monkeypatch.setattr(handlers.vmrun, "is_running", lambda path: False)
    monkeypatch.setattr(handlers.vmx, "patch", lambda path, updates: calls.append(updates))

    result = handlers.handle_resize(job, NullContext(job))

    assert result["cpus"] == 4
    assert result["restarted"] is False
    assert calls == [{"memSize": 8192, "numvcpus": 4, "cpuid.coresPerSocket": handlers.vmx.cpu_topology(4)}]
    vm.refresh_from_db()
    assert vm.cpus == 4
    assert vm.memory_mb == 8192


def test_resize_running_vm_powers_off_then_on(job, vm, monkeypatch):
    sequence = []
    monkeypatch.setattr(handlers.vmrun, "is_running", lambda path: True)
    monkeypatch.setattr(handlers.vmx, "patch", lambda path, updates: sequence.append("patch"))
    monkeypatch.setattr(handlers.power, "power_off", lambda vm, log, force=False: sequence.append("off"))
    monkeypatch.setattr(handlers.power, "power_on", lambda vm, log, gui=False: sequence.append("on"))

    result = handlers.handle_resize(job, NullContext(job))

    assert sequence == ["off", "patch", "on"]
    assert result["restarted"] is True


def test_resize_does_not_power_on_after_vmx_failure(job, vm, monkeypatch):
    """改 vmx 失败时机器要保持关机——半截状态比停机更危险。"""
    sequence = []
    monkeypatch.setattr(handlers.vmrun, "is_running", lambda path: True)
    monkeypatch.setattr(handlers.power, "power_off", lambda vm, log, force=False: sequence.append("off"))
    monkeypatch.setattr(handlers.power, "power_on", lambda vm, log, gui=False: sequence.append("on"))

    monkeypatch.setattr(
        handlers.vmx, "patch", lambda path, updates: (_ for _ in ()).throw(OSError("disk locked"))
    )

    with pytest.raises(RuntimeError, match="Failed to write vmx"):
        handlers.handle_resize(job, NullContext(job))

    assert "on" not in sequence
    # vmx 改写失败时数据库规格也不能变
    vm.refresh_from_db()
    assert vm.cpus == 2


@pytest.mark.parametrize(
    ("cpus", "memory_mb"),
    [(0, 4096), (129, 4096), (2, 128), (2, 4097)],
)
def test_resize_rejects_invalid_spec(job, vm, monkeypatch, cpus, memory_mb):
    job.payload = {"vm_id": vm.pk, "cpus": cpus, "memory_mb": memory_mb}
    monkeypatch.setattr(handlers.vmrun, "is_running", lambda path: False)
    with pytest.raises(RuntimeError):
        handlers.handle_resize(job, NullContext(job))


def test_resize_missing_vmx_fails(job, vm, monkeypatch):
    import os

    monkeypatch.setattr(os.path, "isfile", lambda path: False)
    monkeypatch.setattr(VirtualMachine, "exists", property(lambda self: False))
    with pytest.raises(RuntimeError, match="vmx not found"):
        handlers.handle_resize(job, NullContext(job))


# ---------------------------------------------------------------------------
# 页面层
# ---------------------------------------------------------------------------
def test_resize_view_enqueues(client, user, vm):
    client.force_login(user)
    response = client.post(f"/vms/{vm.pk}/resize/", {"cpus": 4, "memory_mb": 8192})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["job_id"]
    assert Job.objects.get(pk=data["job_id"]).kind == JobKind.RESIZE


def test_resize_view_rejects_invalid_input(client, user, vm):
    client.force_login(user)
    response = client.post(f"/vms/{vm.pk}/resize/", {"cpus": "abc", "memory_mb": ""})
    assert response.status_code == 400


def test_resize_view_requires_login(vm):
    assert Client().post(f"/vms/{vm.pk}/resize/", {"cpus": 2, "memory_mb": 4096}).status_code == 302


def test_resize_view_missing_vmx(client, user, vm, monkeypatch):
    monkeypatch.setattr(VirtualMachine, "exists", property(lambda self: False))
    client.force_login(user)
    response = client.post(f"/vms/{vm.pk}/resize/", {"cpus": 2, "memory_mb": 4096})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
def test_api_resize(user, vm):
    api = APIClient()
    api.force_authenticate(user=user)
    response = api.post(f"/api/v1/vms/{vm.pk}/resize/", {"cpus": 2, "memory_mb": 2048}, format="json")

    assert response.status_code == 202
    assert response.json()["kind"] == "resize"
    job = Job.objects.get(pk=response.json()["job_id"])
    assert job.kind == JobKind.RESIZE
    assert job.payload["memory_mb"] == 2048


def test_api_resize_rejects_out_of_range(user, vm):
    api = APIClient()
    api.force_authenticate(user=user)
    response = api.post(f"/api/v1/vms/{vm.pk}/resize/", {"cpus": 999, "memory_mb": 2048}, format="json")
    assert response.status_code == 400


def test_api_resize_requires_auth(vm):
    api = APIClient()
    response = api.post(f"/api/v1/vms/{vm.pk}/resize/", {"cpus": 2, "memory_mb": 2048}, format="json")
    assert response.status_code == 401
