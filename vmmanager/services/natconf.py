"""NAT 端口转发与 DHCP 静态绑定。

直接编辑 VMware 的配置文件（Virtual Network Editor 干的也是这件事）：

* NAT：Windows ``C:\\ProgramData\\VMware\\vmnetnat.conf``，
  Linux ``/etc/vmware/vmnet8/nat/nat.conf``。
  转发条目长这样（在 ``[tcp]`` / ``[udp]`` 段里）::

      8080 = 192.168.47.10:80        # 宿主 8080 → 虚拟机 80

  注意与设置键区分：设置键（如 ``timeWaitTimeout = 30``）的左边不是数字，
  右边也不是 ``ip:port``。

* DHCP：Windows ``C:\\ProgramData\\VMware\\vmnetdhcp.conf``，
  Linux ``/etc/vmware/vmnetdhcp.conf``。绑定条目是 ISC dhcpd 的 ``host`` 块::

      host vmm-192-168-47-10 {
          hardware ethernet 00:0C:29:XX:XX:XX;
          fixed-address 192.168.47.10;
      }

改完必须重启对应服务才生效——NAT 服务（``VMware NAT Service``）、
DHCP 服务（``VMware DHCP Service``）。**重启服务需要管理员权限**：
面板进程通常不是管理员，所以重启失败时会把手工执行命令一并写进错误信息，
用户可以在管理员终端里自己跑。

安全措施：每次改动前先把原文件复制为 ``.bak``，写入用「临时文件 + 替换」，
解析失败（结构意外）就不动手。
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from django.conf import settings


class NatConfError(RuntimeError):
    """配置读写或服务重启失败。"""


def _detect_nat_conf() -> str:
    if sys.platform == "win32":
        return r"C:\ProgramData\VMware\vmnetnat.conf"
    return "/etc/vmware/vmnet8/nat/nat.conf"


def _detect_dhcp_conf() -> str:
    if sys.platform == "win32":
        return r"C:\ProgramData\VMware\vmnetdhcp.conf"
    return "/etc/vmware/vmnetdhcp.conf"


def nat_conf_path() -> str:
    return settings.VMM.get("NAT_CONF") or _detect_nat_conf()


def dhcp_conf_path() -> str:
    return settings.VMM.get("DHCP_CONF") or _detect_dhcp_conf()


# ---------------------------------------------------------------------------
# 文件读写（带备份与原子替换）
# ---------------------------------------------------------------------------
def _read(path: str) -> str:
    if not Path(path).exists():
        raise NatConfError(f"配置文件不存在：{path}")
    try:
        return Path(path).read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return Path(path).read_text(encoding="gbk", errors="replace")


def _write(path: str, content: str) -> None:
    """备份 → 原子替换。备份保留上次的样子，改坏了还有得救。"""
    target = Path(path)
    backup = target.with_suffix(target.suffix + ".vmm.bak")
    try:
        shutil.copy2(target, backup)
        tmp = target.with_suffix(target.suffix + ".vmm.tmp")
        tmp.write_text(content, encoding="utf-8", newline="")
        os.replace(tmp, target)
    except PermissionError as exc:
        raise NatConfError(f"没有写入 {path} 的权限（面板进程可能不是管理员）：{exc}") from exc
    except OSError as exc:
        raise NatConfError(f"写入 {path} 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# NAT 转发
# ---------------------------------------------------------------------------
_FORWARD_LINE = re.compile(r"^\s*(\d+)\s*=\s*(\d{1,3}(?:\.\d{1,3}){3}):(\d+)\s*$")


def list_forwards(path: str = "") -> list[dict]:
    """解析 vmnetnat.conf 里的转发条目。配置文件不存在时返回空列表。"""
    path = path or nat_conf_path()
    if not Path(path).exists():
        return []

    result: list[dict] = []
    current_section = ""
    for raw in _read(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        section = re.match(r"^\[(\w+)\]$", line)
        if section:
            current_section = section.group(1).lower()
            continue
        match = _FORWARD_LINE.match(line)
        if match and current_section in ("tcp", "udp"):
            result.append(
                {
                    "proto": current_section,
                    "host_port": int(match.group(1)),
                    "guest_ip": match.group(2),
                    "guest_port": int(match.group(3)),
                }
            )
    return result


def add_forward(proto: str, host_port: int, guest_ip: str, guest_port: int) -> None:
    """追加一条转发。已存在完全相同的条目时直接跳过（幂等）。"""
    proto = proto.lower()
    if proto not in ("tcp", "udp"):
        raise NatConfError("协议只支持 tcp / udp")

    path = nat_conf_path()
    lines = _read(path).splitlines()
    entry = f"{host_port} = {guest_ip}:{guest_port}"

    # 幂等：完全相同的条目已经存在就不用再动文件和服务
    for existing in list_forwards(path):
        if (
            existing["proto"] == proto
            and existing["host_port"] == host_port
            and existing["guest_ip"] == guest_ip
            and existing["guest_port"] == guest_port
        ):
            return

    # 找到目标段落的末尾插进去；段落不存在就在文件末尾补一个
    section_idx = None
    end_idx = None
    for i, raw in enumerate(lines):
        if raw.strip().lower() == f"[{proto}]":
            section_idx = i
        elif section_idx is not None and re.match(r"^\[\w+\]\s*$", raw.strip()):
            end_idx = i
            break
    if section_idx is not None:
        insert_at = end_idx if end_idx is not None else len(lines)
        lines.insert(insert_at, entry)
    else:
        lines.extend(["", f"[{proto}]", entry])

    _write(path, "\n".join(lines).rstrip("\n") + "\n")
    restart_nat_service()


def remove_forward(proto: str, host_port: int, guest_ip: str, guest_port: int) -> None:
    proto = proto.lower()
    path = nat_conf_path()
    lines = _read(path).splitlines()
    pattern = re.compile(rf"^\s*{host_port}\s*=\s*{re.escape(guest_ip)}:{guest_port}\s*$")
    kept = [line for line in lines if not pattern.match(line)]
    if len(kept) == len(lines):
        return  # 没有这条，别人可能已经删过了
    _write(path, "\n".join(kept))
    restart_nat_service()


# ---------------------------------------------------------------------------
# DHCP 静态绑定
# ---------------------------------------------------------------------------
_HOST_BLOCK = re.compile(
    r"(?m)^host\s+(\S+)\s*\{[^}]*?"
    r"hardware\s+ethernet\s+([0-9A-Fa-f:.-]+);[^}]*?"
    r"fixed-address\s+(\d{1,3}(?:\.\d{1,3}){3});[^}]*\}",
)


def list_bindings(path: str = "") -> list[dict]:
    """解析 vmnetdhcp.conf 里的 host 静态绑定块。"""
    path = path or dhcp_conf_path()
    if not Path(path).exists():
        return []

    text = _read(path)
    result = []
    for match in _HOST_BLOCK.finditer(text):
        result.append(
            {
                "name": match.group(1),
                "mac": match.group(2).replace("-", ":").upper(),
                "ip": match.group(3),
            }
        )
    return result


def _binding_block(name: str, mac: str, ip: str) -> str:
    return f"host {name} {{\n    hardware ethernet {mac};\n    fixed-address {ip};\n}}\n"


def add_dhcp_binding(mac: str, ip: str) -> None:
    """追加一个 host 绑定块。同一 IP / MAC 已绑定时直接跳过。"""
    mac = mac.replace("-", ":").upper()
    path = dhcp_conf_path()

    for existing in list_bindings(path):
        if existing["ip"] == ip:
            if existing["mac"] == mac:
                return
            raise NatConfError(
                f"IP {ip} 已绑定到 {existing['mac']}（host {existing['name']}），请先删除原绑定"
            )
        if existing["mac"] == mac:
            raise NatConfError(f"MAC {mac} 已绑定到 {existing['ip']}（host {existing['name']}）")

    # ISC dhcpd 的 host 名不能有特殊字符；用 IP 命名最直观
    name = "vmm-" + ip.replace(".", "-")
    text = _read(path)
    if not text.endswith("\n"):
        text += "\n"
    text += "\n" + _binding_block(name, mac, ip)
    _write(path, text)
    restart_dhcp_service()


def remove_dhcp_binding(mac: str) -> None:
    """按 MAC 删除绑定块。VMware 自带的 vmnet host 块（00:50:56:C0:00:xx）受保护。"""
    mac = mac.replace("-", ":").upper()
    if mac.startswith("00:50:56:C0:00:"):
        raise NatConfError("这是 VMnet 宿主机适配器的保留 MAC，不允许删除")

    path = dhcp_conf_path()
    text = _read(path)
    pattern = re.compile(
        r"(?m)^host\s+\S+\s*\{[^}]*?"
        rf"hardware\s+ethernet\s+{re.escape(mac)};[^}}]*?\}}\n?",
        re.IGNORECASE,
    )
    new_text, count = pattern.subn("", text)
    if count == 0:
        return
    _write(path, new_text)
    restart_dhcp_service()


# ---------------------------------------------------------------------------
# 服务重启
# ---------------------------------------------------------------------------
def _is_admin() -> bool:
    if sys.platform == "win32":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def _restart_windows_service(service: str) -> None:
    """Windows 下重启 VMware 服务。需要管理员权限。"""
    if not _is_admin():
        raise NatConfError(
            f"配置已写入，但重启 {service} 需要管理员权限。"
            f'请在管理员终端执行：net stop "{service}" && net start "{service}"'
        )
    for args in (["net", "stop", service], ["net", "start", service]):
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NatConfError(f"重启服务 {service} 失败：{exc}") from exc
        if completed.returncode != 0:
            raise NatConfError(
                f"重启服务 {service} 失败：{(completed.stderr or completed.stdout or '').strip()}"
            )


def _restart_linux_network() -> None:
    try:
        completed = subprocess.run(
            ["vmware-networks", "--stop"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise OSError((completed.stderr or "").strip())
        completed = subprocess.run(
            ["vmware-networks", "--start"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise OSError((completed.stderr or "").strip())
    except FileNotFoundError as exc:
        raise NatConfError(
            "配置已写入，但找不到 vmware-networks。请手动重启 VMware 网络服务使配置生效"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NatConfError(f"重启 VMware 网络失败：{exc}") from exc


def restart_nat_service() -> None:
    if sys.platform == "win32":
        _restart_windows_service("VMware NAT Service")
    else:
        _restart_linux_network()


def restart_dhcp_service() -> None:
    if sys.platform == "win32":
        _restart_windows_service("VMware DHCP Service")
    else:
        _restart_linux_network()
