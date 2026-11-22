"""noVNC 控制台的测试。

覆盖三层：vmx 配置读写、token 签发、控制台页面的三种模式。
不真正启动 websockify / VMware——那两层的集成靠 ``test_vnc_proxy.py``
之外的真机冒烟（README 有说明）。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from vmmanager.models import VirtualMachine, VmTemplate
from vmmanager.services import vnc

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_superuser(
        username="root-user",
        email="",
        password="pw-admin-9x2",
    )


@pytest.fixture
def client(admin):
    c = Client()
    c.force_login(admin)
    return c


@pytest.fixture
def template(db, tmp_path):
    vmx_file = tmp_path / "base.vmx"
    vmx_file.write_text('displayName = "base"\r\n', encoding="utf-8")
    return VmTemplate.objects.create(
        name="base-template",
        vmx_path=str(vmx_file),
        guest_user="root",
        default_cpus=2,
        default_memory_mb=2048,
    )


@pytest.fixture
def vm(db, template, tmp_path):
    vmx_file = tmp_path / "vm-x.vmx"
    vmx_file.write_text('displayName = "vm-x"\r\n', encoding="utf-8")
    return VirtualMachine.objects.create(
        name="vm-x",
        hostname="vm-x",
        vmx_path=str(vmx_file),
        template=template,
        cpus=2,
        memory_mb=2048,
        power_state="stopped",
    )


# ---------------------------------------------------------------------------
# vmx 配置
# ---------------------------------------------------------------------------
def test_config_for_defaults_when_absent(vm):
    cfg = vnc.config_for(vm.vmx_path)
    assert cfg == {"enabled": False, "port": None, "password": ""}


def test_ensure_config_writes_full_vnc_triple(vm):
    result = vnc.ensure_config(vm)

    assert result["changed"] is True
    cfg = result["config"]
    assert cfg["enabled"] is True
    assert isinstance(cfg["port"], int) and 5900 <= cfg["port"] < 6000
    assert len(cfg["password"]) == 8, "VNC 密码上限 8 字符"

    # 写进 vmx 的值必须能原样读回来
    again = vnc.config_for(vm.vmx_path)
    assert again == cfg


def test_ensure_config_is_idempotent(vm):
    first = vnc.ensure_config(vm)
    second = vnc.ensure_config(vm)

    assert first["changed"] is True
    assert second["changed"] is False, "第二次不该再改文件"
    assert second["config"] == first["config"]


def test_ensure_config_avoids_ports_used_by_other_vmx(vm, tmp_path, settings):
    """别的 vmx 已占用的端口不能再分给这台，否则 VMware 后启动的那台失败。"""
    settings.VMM["BASE_DIR"] = str(tmp_path)  # 让端口扫描指向 tmp 下的 vmx
    other = tmp_path / "other.vmx"
    occupied = 5900 + (vm.pk % 90)
    other.write_text(
        f'displayName = "other"\r\nRemoteDisplay.vnc.enabled = "TRUE"\r\n'
        f'RemoteDisplay.vnc.port = "{occupied}"\r\n',
        encoding="utf-8",
    )
    # 让「其它 vmx」落在 BASE_DIR 扫描范围内：直接把它登记成另一个 VM
    VirtualMachine.objects.create(
        name="other",
        hostname="other",
        vmx_path=str(other),
        template=vm.template,
        cpus=1,
        memory_mb=1024,
        power_state="stopped",
    )

    cfg = vnc.ensure_config(vm)["config"]

    assert cfg["port"] != occupied


def test_generate_password_is_alnum():
    for _ in range(20):
        pw = vnc.generate_password()
        assert len(pw) == 8
        assert pw.isalnum()


# ---------------------------------------------------------------------------
# token 签发
# ---------------------------------------------------------------------------
def test_issue_token_writes_target_file(vm, settings):
    vnc.ensure_config(vm)
    token = vnc.issue_token(vm.vmx_path)

    assert vnc.validate_token(token)
    token_file = settings.DATA_DIR / "vnc_tokens" / f"{token}.tok"
    assert token_file.exists()
    host, port = token_file.read_text().strip().split(": ")[1].rsplit(":", 1)
    assert host == "127.0.0.1"
    assert int(port) == vnc.config_for(vm.vmx_path)["port"]


def test_issue_token_purges_stale_files(vm, settings):
    import os
    import time

    vnc.ensure_config(vm)
    token_dir = settings.DATA_DIR / "vnc_tokens"
    token_dir.mkdir(exist_ok=True)
    stale = token_dir / ("0" * 32 + ".tok")
    stale.write_text("0" * 32 + ": 127.0.0.1:1\n")
    old = time.time() - settings.VMM["VNC_TOKEN_TTL"] - 60
    os.utime(stale, (old, old))

    vnc.issue_token(vm.vmx_path)

    assert not stale.exists(), "过期 token 必须被清理"


def test_issue_token_requires_enabled_vnc(vm):
    with pytest.raises(RuntimeError):
        vnc.issue_token(vm.vmx_path)


# ---------------------------------------------------------------------------
# 控制台页面
# ---------------------------------------------------------------------------
def test_console_offline_mode(client, vm):
    resp = client.get(f"/vms/{vm.pk}/console/")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "虚拟机没有在运行" in html
    assert "ws://" not in html, "没运行就不该签发 token"


def test_console_needs_restart_when_vnc_not_listening(client, vm, monkeypatch):
    """VM 运行中但 VNC 端口没监听（配置是运行中写入的）：必须提示重启。"""
    monkeypatch.setattr(vnc, "is_listening", lambda port: False)
    VirtualMachine.objects.filter(pk=vm.pk).update(power_state="running")
    vnc.ensure_config(vm)
    resp = client.get(f"/vms/{vm.pk}/console/")
    html = resp.content.decode()
    assert "需要完整开关机" in html
    assert "ws://" not in html


def test_console_ready_mode_issues_token(client, vm, monkeypatch):
    """端口真的在监听才发 token——页面把 VNC 密码交给 noVNC 完成认证。"""
    monkeypatch.setattr(vnc, "is_listening", lambda port: True)
    VirtualMachine.objects.filter(pk=vm.pk).update(power_state="running")
    vnc.ensure_config(vm)

    resp = client.get(f"/vms/{vm.pk}/console/")
    html = resp.content.decode()

    assert resp.status_code == 200
    assert "ws://" in html
    assert "data-vnc-password" in html
    password = vnc.config_for(vm.vmx_path)["password"]
    assert password in html, "页面要把 VNC 密码交给 noVNC 完成认证"


def test_console_recreate_enqueues_stop_then_start(client, vm, monkeypatch):
    """needs-restart 页面的一键修复：排队 关机 → 开机（开机时自动补 VNC 配置）。"""
    from vmmanager.models import Job, JobKind

    monkeypatch.setattr(vnc, "is_listening", lambda port: False)
    VirtualMachine.objects.filter(pk=vm.pk).update(power_state="running")

    resp = client.post(f"/vms/{vm.pk}/console/")

    assert resp.status_code == 302
    kinds = list(Job.objects.filter(vm=vm).order_by("id").values_list("kind", flat=True))
    assert kinds == [JobKind.POWER_OFF, JobKind.POWER_ON], kinds
