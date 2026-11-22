"""主机与虚拟机资源指标。

不引入 psutil，也不依赖 ``wmic``（Windows 11 24H2 起已移除）。

* 内存：Windows 走 ``ctypes`` 调 ``GlobalMemoryStatusEx``，Linux 读 ``/proc/meminfo``
* CPU：Windows 用 ``GetSystemTimes`` 采样两次算差值，Linux 用 ``getloadavg``
* 进程内存：Windows 走 ``Get-CimInstance``（带缓存），Linux 走 ``ps``

所有指标取不到就返回 ``None``，页面显示「—」而不是报错 —— 指标只是辅助信息。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time

from vmmanager.models import VirtualMachine

from . import vmrun

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: 指标缓存，避免每次页面渲染都去采样 CPU
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 5.0


def _cached(key: str, builder):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = builder()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


# ---------------------------------------------------------------------------
# Windows 原生调用
# ---------------------------------------------------------------------------
class _MemoryStatusEx:
    """``MEMORYSTATUSEX`` 结构体。"""

    def __init__(self):
        import ctypes

        class Struct(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        self._ctypes = ctypes
        self.struct = Struct()
        self.struct.dwLength = ctypes.sizeof(Struct)

    def read(self):
        kernel32 = self._ctypes.windll.kernel32
        if not kernel32.GlobalMemoryStatusEx(self._ctypes.byref(self.struct)):
            raise OSError("GlobalMemoryStatusEx call failed")
        return self.struct


def _windows_memory() -> dict:
    try:
        status = _MemoryStatusEx().read()
    except Exception:
        return {}
    total_mb = int(status.ullTotalPhys / (1024 * 1024))
    avail_mb = int(status.ullAvailPhys / (1024 * 1024))
    return {
        "memory_total_mb": total_mb,
        "memory_used_mb": max(0, total_mb - avail_mb),
        "memory_percent": float(status.dwMemoryLoad),
    }


def _windows_cpu(sample_seconds: float = 0.25) -> float | None:
    """用 GetSystemTimes 采样两次算 CPU 占用。"""
    try:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong),
            ]

        def snapshot():
            idle = FileTime()
            kernel = FileTime()
            user = FileTime()
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            if not ok:
                raise OSError("GetSystemTimes call failed")

            def value(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            return value(idle), value(kernel), value(user)

        idle1, kernel1, user1 = snapshot()
        time.sleep(sample_seconds)
        idle2, kernel2, user2 = snapshot()

        idle_delta = idle2 - idle1
        kernel_delta = kernel2 - kernel1
        user_delta = user2 - user1
        total = kernel_delta + user_delta
        if total <= 0:
            return None
        busy = total - idle_delta
        return round(max(0.0, min(100.0, busy / total * 100)), 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def host_metrics() -> dict:
    """主机 CPU / 内存 / 虚拟机数量（带 5 秒缓存）。"""
    return _cached("host", _build_host_metrics)


def _build_host_metrics() -> dict:
    data = {
        "cpu_percent": None,
        "memory_total_mb": None,
        "memory_used_mb": None,
        "memory_percent": None,
        "vm_count": None,
        "vm_running": None,
    }

    if os.name == "nt":
        data.update(_windows_memory())
        data["cpu_percent"] = _windows_cpu()
    else:
        try:
            meminfo: dict[str, str] = {}
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    key, _, value = line.partition(":")
                    meminfo[key.strip()] = value.strip()
            total_mb = int(meminfo.get("MemTotal", "0 kB").split()[0]) // 1024
            avail_mb = int(meminfo.get("MemAvailable", "0 kB").split()[0]) // 1024
            data["memory_total_mb"] = total_mb
            data["memory_used_mb"] = max(0, total_mb - avail_mb)
            if total_mb:
                data["memory_percent"] = round(data["memory_used_mb"] / total_mb * 100, 1)
            cores = os.cpu_count() or 1
            data["cpu_percent"] = round(min(100.0, os.getloadavg()[0] / cores * 100), 1)
        except (OSError, ValueError, IndexError, AttributeError):
            pass

    data["vm_count"] = VirtualMachine.objects.count()
    data["vm_running"] = VirtualMachine.objects.filter(power_state="running").count()
    return data


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = proc.stdout or proc.stderr
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def vm_process_map() -> dict[str, dict]:
    """把 ``vmware-vmx`` 进程映射到 vmx 路径，取 PID 与常驻内存。

    只用于详情页展示实际占用；取不到就返回空字典，页面不显示这一行。
    """
    return _cached("processes", _build_process_map)


def _build_process_map() -> dict[str, dict]:
    result: dict[str, dict] = {}

    if os.name == "nt":
        # wmic 已被 Windows 11 移除，改用 CIM（首次约 1 秒，有缓存）
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='vmware-vmx.exe'\" "
            "| Select-Object ProcessId,WorkingSetSize,CommandLine | ConvertTo-Json -Compress"
        )
        text = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30)
        if not text.strip():
            return result
        import json

        try:
            payload = json.loads(text)
        except ValueError:
            return result
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            vmx_path = _find_vmx([str(item.get("CommandLine") or "")])
            if not vmx_path:
                continue
            result[vmrun.normpath(vmx_path)] = {
                "pid": item.get("ProcessId"),
                "rss_bytes": item.get("WorkingSetSize") or 0,
            }
    else:
        text = _run(["ps", "-eo", "pid,rss,args"])
        for line in text.splitlines():
            if "vmware-vmx" not in line:
                continue
            match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
            if not match:
                continue
            pid, rss_kb, args = match.groups()
            vmx_path = _find_vmx([args])
            if vmx_path:
                result[vmrun.normpath(vmx_path)] = {
                    "pid": int(pid),
                    "rss_bytes": int(rss_kb) * 1024,
                }
    return result


def _find_vmx(parts: list[str]) -> str:
    """从命令行里挑出 .vmx 路径（可能带引号）。"""
    for part in parts:
        for token in re.findall(r'"[^"]*\.vmx"|\S+\.vmx', str(part), re.IGNORECASE):
            return token.strip('"')
    return ""


def vm_metrics(vm: VirtualMachine) -> dict:
    """单台虚拟机的运行指标。"""
    info = {"running": False, "pid": None, "memory_mb": None}
    if vm.power_state != "running":
        return info

    process = vm_process_map().get(vmrun.normpath(vm.vmx_path))
    if not process:
        return info

    info["running"] = True
    info["pid"] = process.get("pid")
    if process.get("rss_bytes"):
        info["memory_mb"] = round(process["rss_bytes"] / (1024 * 1024))
    return info
