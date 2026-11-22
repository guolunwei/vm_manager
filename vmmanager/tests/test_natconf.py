"""NAT 端口转发与 DHCP 静态绑定的回归测试。

安全红线：改的是宿主的真实网络配置文件，所以
* 每次改动必须留 .vmm.bak 备份；
* 解析出来的条目必须与文件里的真实格式（vmnetnat.conf / vmnetdhcp.conf）一致；
* 服务重启失败要给出「配置已写入 + 手工命令」，不能假装成功。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from vmmanager.services import natconf

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_superuser(username="root-user", email="", password="pw-nat-admin")


@pytest.fixture
def client(admin):
    c = Client()
    c.force_login(admin)
    return c


NAT_SAMPLE = """# Windows NAT configuration file

[host]
ip = 192.168.17.2/24

[tcp]
# Value of timeout in TCP TIME_WAIT state, in seconds
timeWaitTimeout = 30
8080 = 192.168.47.10:80
8443 = 192.168.47.11:443

[udp]
timeout = 30
5353 = 192.168.47.10:5353

[dns]
policy = order
"""

DHCP_SAMPLE = """# Windows DHCP configuration file

subnet 192.168.47.0 netmask 255.255.255.0 {}

host VMnet8 {
    hardware ethernet 00:50:56:C0:00:08;
    fixed-address 192.168.17.1;
    option routers 0.0.0.0;
}

host vmm-192-168-47-10 {
    hardware ethernet 00:0C:29:AA:BB:CC;
    fixed-address 192.168.47.10;
}
"""


@pytest.fixture
def nat_conf(tmp_path, settings):
    path = tmp_path / "vmnetnat.conf"
    path.write_text(NAT_SAMPLE, encoding="utf-8")
    settings.VMM["NAT_CONF"] = str(path)
    return path


@pytest.fixture
def dhcp_conf(tmp_path, settings):
    path = tmp_path / "vmnetdhcp.conf"
    path.write_text(DHCP_SAMPLE, encoding="utf-8")
    settings.VMM["DHCP_CONF"] = str(path)
    return path


@pytest.fixture(autouse=True)
def _no_service_restart(monkeypatch):
    """单测不真的重启服务；重启被单独测（断言报错文案）。"""
    monkeypatch.setattr(natconf, "restart_nat_service", lambda: None)
    monkeypatch.setattr(natconf, "restart_dhcp_service", lambda: None)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_list_forwards_parses_real_format(nat_conf):
    forwards = natconf.list_forwards()

    assert {
        "proto": "tcp",
        "host_port": 8080,
        "guest_ip": "192.168.47.10",
        "guest_port": 80,
    } in forwards
    assert {
        "proto": "udp",
        "host_port": 5353,
        "guest_ip": "192.168.47.10",
        "guest_port": 5353,
    } in forwards
    # 设置键（timeWaitTimeout / timeout / ip）绝不能被误认成转发条目
    assert len(forwards) == 3


def test_list_forwards_missing_file(tmp_path, settings):
    settings.VMM["NAT_CONF"] = str(tmp_path / "nope.conf")
    assert natconf.list_forwards() == []


def test_list_bindings_parses_host_blocks(dhcp_conf):
    bindings = natconf.list_bindings()

    assert {"name": "vmm-192-168-47-10", "mac": "00:0C:29:AA:BB:CC", "ip": "192.168.47.10"} in bindings
    # VMware 自己的 VMnet 宿主适配器块也在列表里（只展示，不允许删）
    assert {"name": "VMnet8", "mac": "00:50:56:C0:00:08", "ip": "192.168.17.1"} in bindings


# ---------------------------------------------------------------------------
# NAT 转发增删
# ---------------------------------------------------------------------------
def test_add_forward_writes_to_section_and_restarts(nat_conf, monkeypatch):
    calls = []
    monkeypatch.setattr(natconf, "restart_nat_service", lambda: calls.append("restart"))

    natconf.add_forward("tcp", 9090, "192.168.47.20", 3000)

    assert calls == ["restart"]
    text = nat_conf.read_text(encoding="utf-8")
    assert "9090 = 192.168.47.20:3000" in text
    # 必须落在 [tcp] 段里（在 [udp] 之前）
    assert text.index("9090 =") < text.index("[udp]")
    # 备份文件存在
    assert nat_conf.with_suffix(".conf.vmm.bak").exists()


def test_add_forward_is_idempotent(nat_conf):
    natconf.add_forward("tcp", 8080, "192.168.47.10", 80)
    # 完全相同的条目已存在：文件保持原样（不会出现两行 8080）
    text = nat_conf.read_text(encoding="utf-8")
    assert text.count("8080 =") == 1


def test_remove_forward(nat_conf):
    natconf.remove_forward("tcp", 8080, "192.168.47.10", 80)
    text = nat_conf.read_text(encoding="utf-8")
    assert "8080 = 192.168.47.10:80" not in text
    assert "8443 = 192.168.47.11:443" in text  # 其它条目不受影响


def test_remove_forward_missing_is_noop(nat_conf):
    natconf.remove_forward("tcp", 1234, "192.168.47.10", 80)  # 不抛异常即可


# ---------------------------------------------------------------------------
# DHCP 绑定增删
# ---------------------------------------------------------------------------
def test_add_dhcp_binding(dhcp_conf, monkeypatch):
    calls = []
    monkeypatch.setattr(natconf, "restart_dhcp_service", lambda: calls.append("restart"))

    natconf.add_dhcp_binding("00:0C:29:11:22:33", "192.168.47.20")

    assert calls == ["restart"]
    text = dhcp_conf.read_text(encoding="utf-8")
    assert "host vmm-192-168-47-20 {" in text
    assert "hardware ethernet 00:0C:29:11:22:33;" in text
    assert "fixed-address 192.168.47.20;" in text


def test_add_dhcp_binding_idempotent_for_same_pair(dhcp_conf):
    natconf.add_dhcp_binding("00:0C:29:AA:BB:CC", "192.168.47.10")
    assert dhcp_conf.read_text(encoding="utf-8").count("host vmm-192-168-47-10") == 1


def test_add_dhcp_binding_rejects_conflicting_ip(dhcp_conf):
    with pytest.raises(natconf.NatConfError, match="已绑定"):
        natconf.add_dhcp_binding("00:0C:29:11:22:33", "192.168.47.10")


def test_add_dhcp_binding_rejects_conflicting_mac(dhcp_conf):
    with pytest.raises(natconf.NatConfError, match="已绑定"):
        natconf.add_dhcp_binding("00:0C:29:AA:BB:CC", "192.168.47.99")


def test_remove_dhcp_binding(dhcp_conf):
    natconf.remove_dhcp_binding("00:0c:29:aa:bb:cc")  # 大小写不敏感
    text = dhcp_conf.read_text(encoding="utf-8")
    assert "vmm-192-168-47-10" not in text
    assert "host VMnet8 {" in text


def test_remove_dhcp_binding_protects_vmnet_host_adapter(dhcp_conf):
    with pytest.raises(natconf.NatConfError, match="保留 MAC"):
        natconf.remove_dhcp_binding("00:50:56:C0:00:08")


# ---------------------------------------------------------------------------
# 服务重启
# ---------------------------------------------------------------------------
def test_restart_without_admin_gives_manual_command(monkeypatch):
    monkeypatch.setattr(natconf, "_is_admin", lambda: False)
    monkeypatch.setattr(natconf.sys, "platform", "win32")

    with pytest.raises(natconf.NatConfError, match="net stop") as excinfo:
        natconf._restart_windows_service("VMware NAT Service")
    # 错误信息里必须带可复制的手工命令
    assert "VMware NAT Service" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 页面层
# ---------------------------------------------------------------------------
def test_forward_add_view(client, nat_conf):
    response = client.post(
        "/network/forwards/add/",
        {"proto": "tcp", "host_port": "9090", "guest_ip": "192.168.47.20", "guest_port": "3000"},
    )
    assert response.status_code == 302
    text = nat_conf.read_text(encoding="utf-8")
    assert "9090 = 192.168.47.20:3000" in text


def test_forward_add_view_rejects_bad_input(client, nat_conf):
    response = client.post(
        "/network/forwards/add/",
        {"proto": "tcp", "host_port": "abc", "guest_ip": "192.168.47.20", "guest_port": "3000"},
    )
    assert response.status_code == 302
    # 非法输入不能写文件
    assert "9090" not in nat_conf.read_text(encoding="utf-8")


def test_network_views_require_staff(tmp_path, nat_conf):
    get_user_model().objects.create_user(username="plain", password="pw-plain")
    client = Client()
    client.force_login(get_user_model().objects.get(username="plain"))
    response = client.post(
        "/network/forwards/add/",
        {"proto": "tcp", "host_port": "9090", "guest_ip": "192.168.47.20", "guest_port": "3000"},
    )
    assert response.status_code == 302
    assert "9090" not in nat_conf.read_text(encoding="utf-8")


def test_binding_delete_view(client, dhcp_conf):
    response = client.post("/network/bindings/delete/", {"mac": "00:0C:29:AA:BB:CC"})
    assert response.status_code == 302
    assert "vmm-192-168-47-10" not in dhcp_conf.read_text(encoding="utf-8")
