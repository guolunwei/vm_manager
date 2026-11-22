"""网络探测与 IP 分配。

虚拟机「到底拿了哪个 IP」是这个项目最容易出错的地方，所以这里做多级兜底：

1. ``vmrun getGuestIPAddress`` —— 最准，但依赖 VMware Tools 正常运行；
2. VMware DHCP 租约文件 —— 适用于 NAT / 自动获取的虚拟机，不依赖 Tools；
3. **ARP 表按 MAC 匹配** —— 最可靠的兜底。虚拟机的 MAC 在 vmx 里写死，
   只要它跟宿主机通过同一块虚拟网卡通过信，ARP 表里就有对应关系，
   完全不需要进客户机；
4. 网段扫描 —— 前三种都失败时，ping 一遍网段刷新 ARP 再匹配。

第 3 条是重点：很多「页面显示 IP 为空 / ping 不通」的问题，根因都是只用了
第 1 条，而模板里 Tools 没起来。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings

from . import vmrun

_MAC_CLEAN = re.compile(r"[^0-9A-Fa-f]")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def normalize_mac(mac: str) -> str:
    """统一成 ``00:0C:29:32:B7:2C`` 形式，方便比较。"""
    cleaned = _MAC_CLEAN.sub("", mac or "").upper()
    if len(cleaned) != 12:
        return (mac or "").upper()
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def ping(host: str, *, timeout_ms: int | None = None) -> bool:
    """跨平台 ping 单个地址，用退出码判断，避免解析本地化输出。"""
    if not host:
        return False
    timeout_ms = timeout_ms or settings.VMM["SCAN_TIMEOUT_MS"]
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=max(3, timeout_ms / 1000 + 3),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def arp_table() -> dict[str, str]:
    """读系统 ARP 缓存，返回 ``{ip: mac}``。"""
    result: dict[str, str] = {}
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["arp", "-a"],
                capture_output=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = _decode(proc.stdout)
        else:
            proc = subprocess.run(["ip", "neigh"], capture_output=True, timeout=20)
            text = _decode(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        return result

    ip_re = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
    mac_re = re.compile(r"\b([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})\b")
    for line in text.splitlines():
        ip_match = ip_re.search(line)
        mac_match = mac_re.search(line)
        if not ip_match or not mac_match:
            continue
        mac = normalize_mac(mac_match.group(1))
        if mac in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"):
            continue
        result[ip_match.group(1)] = mac
    return result


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def ip_by_mac(mac: str) -> str | None:
    mac = normalize_mac(mac)
    if not mac:
        return None
    for ip, found in arp_table().items():
        if found == mac:
            return ip
    return None


def scan_subnet(cidr: str, *, workers: int = 64) -> list[str]:
    """并发 ping 扫网段，返回能 ping 通的地址列表。"""
    if not settings.VMM["SCAN_ENABLED"]:
        return []
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    if network.num_addresses > 1024:
        return []

    hosts = [str(ip) for ip in network.hosts()]
    alive: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for host, ok in zip(hosts, pool.map(ping, hosts), strict=False):
            if ok:
                alive.append(host)
    return alive


def subnet_of(ip: str, prefix: int) -> str:
    try:
        return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
    except ValueError:
        return ""


def same_subnet(a: str, b: str, prefix: int) -> bool:
    net_a, net_b = subnet_of(a, prefix), subnet_of(b, prefix)
    return bool(net_a) and net_a == net_b


# ---------------------------------------------------------------------------
# DHCP 租约
# ---------------------------------------------------------------------------
_LEASE_BLOCKS = re.compile(r"lease\s*\{(?P<body>.*?)\}", re.S)


def dhcp_leases() -> dict[str, str]:
    """解析 VMware 的 DHCP 租约文件，返回 ``{mac: ip}``。

    文件位置随平台而异；找不到就返回空字典，不影响其他探测方式。
    """
    candidates = [
        Path(r"C:\ProgramData\VMware\vmnetdhcp.leases"),
        Path(r"C:\Documents and Settings\All Users\Application Data\VMware\vmnetdhcp.leases"),
        Path("/etc/vmware/vmnetdhcp.leases"),
        Path("/var/lib/vmware/vmnetdhcp.leases"),
        Path("/Library/Preferences/VMware Fusion/vmnetdhcp.leases"),
    ]
    text = ""
    for path in candidates:
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                break
            except OSError:
                continue
    if not text:
        return {}

    leases: dict[str, str] = {}
    for block in _LEASE_BLOCKS.finditer(text):
        body = block.group("body")
        ip_match = re.search(r"fixed-address\s+(\d{1,3}(?:\.\d{1,3}){3})", body)
        mac_match = re.search(r"hardware\s+ethernet\s+([0-9a-fA-F:]{17})", body)
        if ip_match and mac_match:
            leases[normalize_mac(mac_match.group(1))] = ip_match.group(1)
    return leases


# ---------------------------------------------------------------------------
# 虚拟网段（VMnet）
# ---------------------------------------------------------------------------
#: VMware 的固定约定：VMnet1 是 host-only，VMnet8 是 NAT
DEFAULT_VMNET = {"hostonly": 1, "nat": 8}

_DHCP_CONF_CANDIDATES = [
    Path(r"C:\ProgramData\VMware\vmnetdhcp.conf"),
    Path("/etc/vmware/vmnetdhcp.conf"),
    Path("/etc/vmware/vmnet1/dhcp/dhcpd.conf"),
    Path("/Library/Preferences/VMware Fusion/vmnetdhcp.conf"),
]

_SEGMENT = re.compile(r"#\s*Virtual ethernet segment\s+(\d+)", re.I)
_SUBNET = re.compile(r"subnet\s+(\d{1,3}(?:\.\d{1,3}){3})\s+netmask\s+(\d{1,3}(?:\.\d{1,3}){3})")
_RANGE = re.compile(r"range\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,3}(?:\.\d{1,3}){3})")
_FIXED = re.compile(r"host\s+VMnet\d+\s*\{[^}]*?fixed-address\s+(\d{1,3}(?:\.\d{1,3}){3})", re.I | re.S)
_DNS = re.compile(r"option\s+domain-name-servers\s+(\d{1,3}(?:\.\d{1,3}){3})")


def vmnet_dhcp_conf() -> Path | None:
    """找到 VMware 的 DHCP 配置文件。"""
    for path in _DHCP_CONF_CANDIDATES:
        if path.is_file():
            return path
    return None


def _networks_from_conf(text: str) -> dict[int, dict]:
    networks: dict[int, dict] = {}
    marks = [(m.start(), int(m.group(1))) for m in _SEGMENT.finditer(text)]
    if not marks:
        return networks

    for index, (start, vnet) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        chunk = text[start:end]

        subnet = _SUBNET.search(chunk)
        if not subnet:
            continue
        net_addr, mask = subnet.group(1), subnet.group(2)
        try:
            prefix = ipaddress.ip_network(f"{net_addr}/{mask}", strict=False).prefixlen
        except ValueError:
            prefix = 24

        fixed = _FIXED.search(chunk)
        dns = _DNS.search(chunk)
        pool = [m.groups() for m in _RANGE.finditer(chunk)]

        networks[vnet] = {
            "vnet": vnet,
            "subnet": net_addr,
            "prefix": prefix,
            "cidr": f"{net_addr}/{prefix}",
            # host-only 网段的网关就是 VMnet 网卡自己的地址
            "gateway": fixed.group(1) if fixed else "",
            "dns": dns.group(1) if dns else (fixed.group(1) if fixed else ""),
            "dhcp_pools": pool,
        }
    return networks


def vmnet_networks() -> dict[int, dict]:
    """解析 VMware 的 DHCP 配置，返回 ``{vnet 号: 网段信息}``。

    信息包括子网、前缀、网关、DNS 和 DHCP 动态地址池。读不到就返回空字典，
    调用方应当按「探测失败」处理，不要假设默认值。
    """
    conf = vmnet_dhcp_conf()
    if conf is None:
        return {}
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return _networks_from_conf(text)


def vmnet_for(connection_type: str) -> int | None:
    """把 vmx 里的 ``connectionType`` 映射成 VMnet 号。"""
    return DEFAULT_VMNET.get((connection_type or "").strip().lower())


def network_for(connection_type: str) -> dict | None:
    """按连接方式取该虚拟网段的信息，拿不到返回 ``None``。"""
    vnet = vmnet_for(connection_type)
    if vnet is None:
        return None
    return vmnet_networks().get(vnet)


def detect_gateway(connection_type: str = "hostonly") -> tuple[str, str, int]:
    """推断 ``(网关, DNS, 前缀长度)``，用于首次初始化时少填几项配置。

    只对 host-only / NAT 有意义；桥接的网关取决于物理网络，只能人工填。
    """
    info = network_for(connection_type)
    if not info:
        return "", "", 24
    return info.get("gateway", ""), info.get("dns", ""), info.get("prefix", 24)


def _dhcp_pool_for(cidr: str) -> list[tuple[int, int]]:
    """找出该网段的 DHCP 动态池，静态分配时应当避开。

    否则我们手工设的地址和 VMware DHCP 分出去的地址可能撞车。
    """
    try:
        target = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    pools: list[tuple[int, int]] = []
    for info in vmnet_networks().values():
        if info.get("cidr") != str(target):
            continue
        for start, end in info.get("dhcp_pools") or []:
            try:
                pools.append((int(ipaddress.ip_address(start)), int(ipaddress.ip_address(end))))
            except ValueError:
                continue
    return pools


# ---------------------------------------------------------------------------
# 综合探测
# ---------------------------------------------------------------------------
def detect_ip(
    vmx_path: str,
    *,
    mac: str = "",
    cidr: str = "",
    allow_scan: bool = True,
    use_tools: bool = True,
) -> tuple[str | None, str]:
    """探测一台虚拟机的 IP。

    返回 ``(ip 或 None, 探测方式说明)``，方便把「怎么找到的」写进日志，
    排查问题时很有用。
    """
    # 1) VMware Tools
    if use_tools and vmrun.is_running(vmx_path):
        ip = vmrun.get_guest_ip(vmx_path, timeout=25)
        if ip:
            return ip, "vmware-tools"

    # 2) DHCP 租约
    if mac:
        lease = dhcp_leases().get(normalize_mac(mac))
        if lease:
            return lease, "dhcp-lease"

    # 3) ARP 按 MAC 匹配
    if mac:
        found = ip_by_mac(mac)
        if found:
            return found, "arp"

    # 4) 扫网段后重新查 ARP
    if allow_scan and cidr and mac:
        scan_subnet(cidr)
        found = ip_by_mac(mac)
        if found:
            return found, "arp-after-scan"

    return None, "not-found"


# ---------------------------------------------------------------------------
# 地址分配
# ---------------------------------------------------------------------------
def used_ips(cidr: str) -> set[str]:
    """网段内已被占用的地址：ARP 缓存 + 系统已配地址。"""
    used: set[str] = set()
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return used

    for ip in arp_table():
        if ipaddress.ip_address(ip) in network:
            used.add(ip)

    # 再扫一遍，确保 ARP 缓存里没有的也能发现
    used.update(scan_subnet(cidr))
    return used


def next_free_ip(cidr: str, *, reserved: set[str] | None = None) -> str | None:
    """挑一个空闲地址。

    跳过网络地址、广播地址、网关（前两个主机位）和已占用地址，并且**避开
    VMware DHCP 的动态池**——静态地址落进动态池，迟早会和自动获取的虚拟机撞车。
    """
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    if network.num_addresses < 8:
        return None

    skip = {str(network.network_address), str(network.broadcast_address)}
    hosts = list(network.hosts())
    for host in hosts[:2]:  # 常见的网关 / NAT 设备
        skip.add(str(host))

    pools = _dhcp_pool_for(cidr)

    taken = used_ips(cidr) | (reserved or set()) | skip
    for host in hosts:
        value = int(host)
        if any(start <= value <= end for start, end in pools):
            continue
        candidate = str(host)
        if candidate not in taken:
            return candidate
    return None


def tcp_probe(host: str, port: int = 22, timeout: float = 1.5) -> bool:
    """不依赖 ICMP 的连通性判断（有些客户机禁 ping）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(host: str, port: int = 22, *, timeout: int = 120, interval: float = 3.0) -> bool:
    """等某个端口可连，用于「开机后等 SSH 就绪」。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tcp_probe(host, port):
            return True
        time.sleep(interval)
    return False
