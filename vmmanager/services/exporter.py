"""OVF/OVA 导出：把一台虚拟机打包成可迁移的 .ova 文件。

走 VMware 自带的 ``ovftool``（Workstation / Fusion 都有，vSphere 客户端
装了也有）。没有就优雅降级——页面提示未安装，其它功能不受影响。

边界（文档化）：

* 导出要求虚拟机**关机**。ovftool 对运行中的机器也能导，但磁盘快照不
  一致，拿到的镜像可能起不来——这个坑值得替用户避开；
* 导出目标目录在 ``EXPORT_DIR``（默认 ``BASE_DIR/exports``）；
* ovftool 的进度写到 stderr（百分比行），逐行转发到任务日志。
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings

from vmmanager.models import PowerState, VirtualMachine

from . import vmrun

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# ovftool 进度行，如 "Disk Progress: 45%"
_PROGRESS = re.compile(r"(?:Disk|Transfer)?\s*Progress:\s*(\d+)%", re.IGNORECASE)


def export_dir() -> Path:
    """导出目录，默认 ``BASE_DIR/exports``，不存在就创建。"""
    raw = settings.VMM.get("EXPORT_DIR") or str(Path(settings.VMM["BASE_DIR"]) / "exports")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ovftool_path() -> str:
    return str(settings.VMM.get("OVFTOOL") or "")


def _ensure_powered_off(vm: VirtualMachine, ctx) -> None:
    """以 vmrun 实时状态为准，运行中的机器拒绝导出。"""
    try:
        running = vmrun.list_running_set()
        is_running = vmrun.normpath(vm.vmx_path) in running
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Cannot query VM power state: {exc}") from exc
    if is_running or vm.power_state == PowerState.RUNNING:
        raise RuntimeError(
            "VM is powered on. Shut it down before exporting (ovftool needs a consistent disk)"
        )


def export_ova(vm: VirtualMachine, ctx, *, user: str = "", password: str = "") -> dict:
    """执行导出，返回 ``{"path": ..., "size_gb": ...}``。

    ovftool 的用法：``ovftool --acceptAllEulas source.vmx dest.ova``。
    """
    tool = ovftool_path()
    if not tool or not Path(tool).exists():
        raise RuntimeError(
            "ovftool not found. Install VMware Workstation (bundled) or set OVFTOOL in config.ini"
        )

    _ensure_powered_off(vm, ctx)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = export_dir() / f"{vm.name}-{stamp}.ova"
    ctx.info(f"Exporting to {dest}")

    cmd = [tool, "--acceptAllEulas", "--noSSLVerify", vm.vmx_path, str(dest)]
    ctx.info("$ " + " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )
    last_pct = -1
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if not line:
            continue
        match = _PROGRESS.search(line)
        if match:
            pct = int(match.group(1))
            if pct != last_pct and pct % 10 == 0:
                last_pct = pct
                ctx.info(f"Progress: {pct}%")
        elif not line.startswith(("Updating", "Opening", "Loading", "Disk")):
            ctx.info(line[:200])

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ovftool exited with code {rc}, see log above")

    size_gb = dest.stat().st_size / (1024 * 1024 * 1024)
    ctx.ok(f"Exported {vm.name} -> {dest.name} ({size_gb:.2f} GB)")
    return {"path": str(dest), "size_gb": round(size_gb, 2)}
