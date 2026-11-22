"""网络工具与 VMnet 网段解析。"""

from __future__ import annotations

import pytest

from vmmanager.services import network

#: 真实 ``vmnetdhcp.conf`` 的片段（Win11 + Workstation 17）
DHCP_CONF = """\
allow unknown-clients;

# Virtual ethernet segment 1
# Added at 11/21/26 02:14:53
subnet 192.168.47.0 netmask 255.255.255.0 {
range 192.168.47.128 192.168.47.254;            # default allows up to 125 VM's
option broadcast-address 192.168.47.255;
option domain-name-servers 192.168.47.1;
option domain-name "localdomain";
}
host VMnet1 {
    hardware ethernet 00:50:56:C0:00:01;
    fixed-address 192.168.47.1;
    option domain-name-servers 0.0.0.0;
}

# Virtual ethernet segment 8
subnet 192.168.17.0 netmask 255.255.255.0 {
range 192.168.17.128 192.168.17.254;
option domain-name-servers 192.168.17.2;
option routers 192.168.17.2;
}
host VMnet8 {
    hardware ethernet 00:50:56:C0:00:08;
    fixed-address 192.168.17.1;
}
"""


@pytest.fixture
def conf(monkeypatch):
    """让 ``vmnet_networks()`` 读上面这段文本，不依赖本机 VMware。"""
    parsed = network._networks_from_conf(DHCP_CONF)
    monkeypatch.setattr(network, "vmnet_networks", lambda: parsed)
    return parsed


def test_parse_vmnet_segments(conf):
    assert set(conf) == {1, 8}

    hostonly = conf[1]
    assert hostonly["cidr"] == "192.168.47.0/24"
    assert hostonly["prefix"] == 24
    assert hostonly["gateway"] == "192.168.47.1"
    assert hostonly["dns"] == "192.168.47.1"
    assert hostonly["dhcp_pools"] == [("192.168.47.128", "192.168.47.254")]

    nat = conf[8]
    assert nat["gateway"] == "192.168.17.1"
    assert nat["dns"] == "192.168.17.2"


def test_detect_gateway_maps_connection_type(conf):
    """host-only 走 VMnet1、NAT 走 VMnet8 —— 用户不用自己算网段。"""
    assert network.detect_gateway("hostonly") == ("192.168.47.1", "192.168.47.1", 24)
    assert network.detect_gateway("nat") == ("192.168.17.1", "192.168.17.2", 24)


def test_detect_gateway_unknown_type(conf):
    assert network.detect_gateway("bridged") == ("", "", 24)


def test_parse_empty_text():
    assert network._networks_from_conf("") == {}


def test_dhcp_pool_lookup(conf):
    assert network._dhcp_pool_for("192.168.47.0/24") == [
        (0xC0A82F80, 0xC0A82FFE)  # 192.168.47.128 - 192.168.47.254
    ]
    assert network._dhcp_pool_for("10.0.0.0/24") == []


def test_next_free_ip_avoids_dhcp_pool(conf, monkeypatch):
    """静态地址必须落在动态池之外，否则迟早和 DHCP 分出去的地址撞车。"""
    monkeypatch.setattr(network, "used_ips", lambda cidr: set())

    picked = network.next_free_ip("192.168.47.0/24")

    assert picked == "192.168.47.3"
    value = int(network.ipaddress.ip_address(picked))
    start, end = network._dhcp_pool_for("192.168.47.0/24")[0]
    assert not (start <= value <= end)


def test_next_free_ip_respects_reserved(conf, monkeypatch):
    monkeypatch.setattr(network, "used_ips", lambda cidr: set())
    picked = network.next_free_ip("192.168.47.0/24", reserved={"192.168.47.3"})
    assert picked == "192.168.47.4"


def test_next_free_ip_returns_none_when_pool_exhausted(conf, monkeypatch):
    """整个网段只剩动态池里的地址时，宁可返回 None 也不要硬塞一个。"""
    taken = {f"192.168.47.{i}" for i in range(3, 128)}
    monkeypatch.setattr(network, "used_ips", lambda cidr: taken)
    assert network.next_free_ip("192.168.47.0/24") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("00:0c:29:f6:35:ce", "00:0C:29:F6:35:CE"),
        ("00-0C-29-F6-35-CE", "00:0C:29:F6:35:CE"),
        ("000c29f635ce", "00:0C:29:F6:35:CE"),
        ("", ""),
        ("garbage", "GARBAGE"),
    ],
)
def test_normalize_mac(raw, expected):
    assert network.normalize_mac(raw) == expected


def test_subnet_helpers():
    assert network.subnet_of("192.168.47.3", 24) == "192.168.47.0/24"
    assert network.subnet_of("not-an-ip", 24) == ""
    assert network.same_subnet("192.168.47.3", "192.168.47.9", 24)
    assert not network.same_subnet("192.168.47.3", "192.168.48.9", 24)
