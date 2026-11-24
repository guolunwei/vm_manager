"""OVA 导出：ovftool 探测、前置检查与任务执行。"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.urls import reverse

from vmmanager.models import VirtualMachine
from vmmanager.services import exporter

pytestmark = pytest.mark.django_db


@pytest.fixture
def vm(tmp_path):
    vmx = tmp_path / "vm-exp.vmx"
    vmx.write_text("# fake vmx")
    return VirtualMachine.objects.create(
        name="vm-exp",
        vmx_path=str(vmx),
        cpus=1,
        memory_mb=1024,
    )


@pytest.fixture
def fake_tool(tmp_path, monkeypatch):
    """伪造一个 ovftool 可执行文件，并把它写进 settings。"""
    tool = tmp_path / "ovftool.exe"
    tool.write_text("fake")
    from django.conf import settings as dj_settings

    monkeypatch.setitem(dj_settings.VMM, "OVFTOOL", str(tool))
    monkeypatch.setitem(dj_settings.VMM, "EXPORT_DIR", str(tmp_path / "exports"))
    return tool


class NullCtx:
    msgs: list = []

    def info(self, m):
        self.msgs.append(m)

    def ok(self, m):
        self.msgs.append(m)

    def warn(self, m):
        self.msgs.append(m)

    def check_cancel(self):
        pass


def test_ovftool_missing_is_empty_string(monkeypatch):
    from django.conf import settings as dj_settings

    monkeypatch.setitem(dj_settings.VMM, "OVFTOOL", "")
    assert exporter.ovftool_path() == ""


def test_export_dir_defaults_under_base_dir(fake_tool):
    d = exporter.export_dir()
    assert d.exists()
    assert d.name == "exports"


def test_export_refuses_running_vm(vm, fake_tool, monkeypatch):
    monkeypatch.setattr(exporter.vmrun, "list_running_set", lambda: {exporter.vmrun.normpath(vm.vmx_path)})
    with pytest.raises(RuntimeError, match="[Pp]owered on"):
        exporter.export_ova(vm, NullCtx())


def test_export_without_tool_raises(vm, monkeypatch):
    from django.conf import settings as dj_settings

    monkeypatch.setitem(dj_settings.VMM, "OVFTOOL", "")
    with pytest.raises(RuntimeError, match="ovftool"):
        exporter.export_ova(vm, NullCtx())


def test_export_runs_ovftool_and_names_file(vm, fake_tool, monkeypatch):
    monkeypatch.setattr(exporter.vmrun, "list_running_set", lambda: set())

    calls = {}

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd
        dest = Path(cmd[-1])
        dest.write_bytes(b"OVA")  # 伪造成品

        class P:
            stdout = iter(["Disk Progress: 50%\n", "Disk Progress: 100%\n"])

            def wait(self):
                return 0

        return P()

    monkeypatch.setattr(exporter.subprocess, "Popen", fake_popen)
    ctx = NullCtx()
    result = exporter.export_ova(vm, ctx)

    cmd = calls["cmd"]
    assert cmd[0] == str(fake_tool)
    assert cmd[-2] == vm.vmx_path
    assert cmd[-1].endswith(".ova")
    assert vm.name in Path(cmd[-1]).name
    assert result["size_gb"] >= 0
    assert any("50%" in m for m in ctx.msgs)


def test_export_nonzero_exit_raises(vm, fake_tool, monkeypatch):
    monkeypatch.setattr(exporter.vmrun, "list_running_set", lambda: set())

    def fake_popen(cmd, **kwargs):
        class P:
            stdout = iter(["Some error\n"])

            def wait(self):
                return 1

        return P()

    monkeypatch.setattr(exporter.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError, match="exited with code 1"):
        exporter.export_ova(vm, NullCtx())


def test_web_export_enqueue(client, vm, fake_tool, monkeypatch):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="exp-user", password="pw-12345678", is_staff=True)
    client.force_login(user)
    monkeypatch.setattr(exporter.vmrun, "list_running_set", lambda: set())
    response = client.post(reverse("vm_export", kwargs={"pk": vm.pk}))
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"]
    assert payload["job_id"] > 0


def test_web_export_requires_login(client, vm):
    response = client.post(reverse("vm_export", kwargs={"pk": vm.pk}))
    assert response.status_code in (302, 401, 403)
