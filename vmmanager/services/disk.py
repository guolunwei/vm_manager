"""虚拟磁盘：容量解析与扩容。

扩容链路分两段，缺一不可：

1. **VMware 侧**：``vmware-vdiskmanager -x <size> <disk.vmdk>`` 把虚拟磁盘
   的容量改大。必须在**关机状态**执行，且只对「可增长」的磁盘有效
   （预分配磁盘会报错，把原样错误返回给用户）。
2. **Guest 侧**：磁盘变大了，Guest 里的分区和文件系统不会自动跟着变。
   通过 Guest Operations 尝试 ``growpart`` + ``resize2fs`` / ``xfs_growfs``。
   这一步是尽力而为（best-effort）——系统盘布局千差万别（LVM、多分区、
   云工具缺失都可能），失败只警告不判定任务失败，磁盘本身已经扩成功了。

约束（在 handler 里校验，这里只做纯操作）：

* 链接克隆的磁盘是模板基础盘的 overlay，**不能扩容**——要扩先把这台
  机器做成完整克隆。
* 有快照时先删快照（快照链上的 delta 磁盘容量受限于基础盘）。
"""

from __future__ import annotations

import re
import struct
import subprocess
from pathlib import Path

from django.conf import settings

from .vmx import disk_files


class DiskError(RuntimeError):
    """磁盘操作失败。"""


# ---------------------------------------------------------------------------
# 工具路径
# ---------------------------------------------------------------------------
def _detect_vdiskmanager() -> str:
    """vmware-vdiskmanager 与 vmrun 同目录，跟着 VMRUN 的探测结果找。"""
    vmrun_path = settings.VMM.get("VMRUN") or ""
    if vmrun_path:
        sibling = Path(vmrun_path).parent / "vmware-vdiskmanager.exe"
        if sibling.exists():
            return str(sibling)
    candidates = [
        r"C:\Program Files (x86)\VMware\VMware Workstation\vmware-vdiskmanager.exe",
        r"C:\Program Files\VMware\VMware Workstation\vmware-vdiskmanager.exe",
        "/usr/bin/vmware-vdiskmanager",
        "/usr/local/bin/vmware-vdiskmanager",
        "/Applications/VMware Fusion.app/Contents/Library/vmware-vdiskmanager",
    ]
    for item in candidates:
        if Path(item).exists():
            return item
    return "vmware-vdiskmanager"


def vdiskmanager_path() -> str:
    """配置优先；没配置就按 VMRUN 所在目录自动探测。"""
    return settings.VMM.get("VDISKMANAGER") or _detect_vdiskmanager()


# ---------------------------------------------------------------------------
# 容量解析
# ---------------------------------------------------------------------------
_RW_LINE = re.compile(r"^RW\s+(\d+)\s+(?:FLAT|SPARSE|VMFS|ZERO)", re.IGNORECASE)

#: 文本描述符通常只有几 KB；超过这个大小就按二进制数据盘处理。
#: 单体 sparse 盘的 .vmdk 本身就是数据文件（可达数 GB），整份读进内存会直接
#: 撑爆进程——详情页每刷新一次就复现一次。
_DESCRIPTOR_MAX_BYTES = 256 * 1024

#: 单体 sparse 盘的头部魔数（KDMV），后面紧跟的字段里有精确的虚拟容量。
_SPARSE_MAGIC = b"KDMV"


def _sparse_capacity_bytes(path: Path) -> int:
    """从单体 sparse 盘头部读虚拟容量（字节），读不到返回 0。

    header 布局：magic(4) version(4) flags(4) capacity(8, 扇区数) ...
    只读 512 字节，不会把 GB 级的数据盘拖进内存。
    """
    try:
        with path.open("rb") as fh:
            header = fh.read(512)
    except OSError:
        return 0
    if len(header) < 20 or header[:4] != _SPARSE_MAGIC:
        return 0
    sectors = struct.unpack_from("<Q", header, 12)[0]
    return sectors * 512


def disk_capacity_bytes(vmx_path: str) -> int:
    """解析第一块磁盘的虚拟容量（字节）。

    .vmdk 描述符是纯文本，每行 ``RW <扇区数> FLAT|SPARSE "文件"``，
    总容量 = 扇区数之和 × 512。解析失败时退回「找最大的磁盘相关文件」，
    对单体 sparse 盘这刚好等于虚拟容量，对预分配盘也够用。
    """
    disks = disk_files(vmx_path)
    if not disks:
        return 0

    descriptor = Path(disks[0])
    if not descriptor.exists():
        return 0

    try:
        descriptor_size = descriptor.stat().st_size
    except OSError:
        return 0

    total_sectors = 0
    # 只有小文件才可能是文本描述符；单体 sparse 盘跳过解析，直接走下面的
    # 「按文件大小兜底」分支（对这类盘，文件大小就是虚拟容量的近似下界）。
    if descriptor_size <= _DESCRIPTOR_MAX_BYTES:
        try:
            text = descriptor.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                match = _RW_LINE.match(line.strip())
                if match:
                    total_sectors += int(match.group(1))
        except OSError:
            return 0

    if total_sectors:
        return total_sectors * 512

    # 单体 sparse 盘：头部有精确容量，先读它（512 字节，零成本）
    sparse = _sparse_capacity_bytes(descriptor)
    if sparse:
        return sparse

    # 描述符解析不出来：按文件大小兜底（sparse 盘的文件大小 ≈ 已用容量，
    # 只是「至少这么大」的下界，页面上要如实标注）
    directory = descriptor.parent
    stem = descriptor.name
    sizes = []
    try:
        for entry in directory.iterdir():
            if entry.name == stem or entry.name.startswith(descriptor.stem):
                if entry.is_file():
                    sizes.append(entry.stat().st_size)
    except OSError:
        pass
    return max(sizes, default=0)


def disk_capacity_gb(vmx_path: str) -> float:
    """容量换算成 GB，保留一位小数。"""
    return round(disk_capacity_bytes(vmx_path) / (1024**3), 1)


# ---------------------------------------------------------------------------
# 扩容
# ---------------------------------------------------------------------------
def grow_disk(vmx_path: str, new_size_gb: int, log) -> None:
    """把磁盘容量扩到 ``new_size_gb``。必须已经关机。"""
    disks = disk_files(vmx_path)
    if not disks:
        raise DiskError("The vmx does not reference any virtual disk")

    tool = vdiskmanager_path()
    current = disk_capacity_gb(vmx_path)
    log(f"Resizing disk {Path(disks[0]).name} from ~{current} GB to {new_size_gb} GB")

    command = [tool, "-x", f"{new_size_gb}GB", disks[0]]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise DiskError(f"vmware-vdiskmanager not found ({tool}); set VDISKMANAGER in config") from exc
    except subprocess.TimeoutExpired as exc:
        raise DiskError("vmware-vdiskmanager timed out (15 min)") from exc

    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if completed.returncode != 0:
        # 常见原因：磁盘不可增长（预分配）、有快照链、权限不足
        tail = "\n".join(output.splitlines()[-5:]) or f"exit code {completed.returncode}"
        raise DiskError(f"Disk resize failed: {tail}")

    log(f"Disk resized to {new_size_gb} GB")


# ---------------------------------------------------------------------------
# Guest 内扩分区（best-effort）
# ---------------------------------------------------------------------------
# 约束写在脚本里而不是 Python 里：Guest 里装了什么工具只有它自己知道。
# - growpart 属于 cloud-utils-growpart；没有就用 fdisk 兜底的话风险太大，直接跳过。
# - 文件系统两选一：xfs 用 xfs_growfs，其余（ext4 等）用 resize2fs。
_GROW_SCRIPT = r"""
set -u
root_dev=$(findmnt -no SOURCE / 2>/dev/null || true)
if [ -z "$root_dev" ]; then
    echo "SKIP: cannot determine root device"
    exit 0
fi
echo "root device: $root_dev"

# LVM / mapper 设备没有 growpart 的分区概念，直接扩 PV 也超出脚本能力
case "$root_dev" in
    *mapper*|*/dm-*) echo "SKIP: device-mapper/LVM layout not handled"; exit 0 ;;
esac

# /dev/sda1 -> /dev/sda + 1
case "$root_dev" in
    *[0-9]p[0-9]*) disk="${root_dev%[0-9]p[0-9]*}"; part="${root_dev##*p}" ;;
    *[0-9]) disk="${root_dev%[0-9]}"; part="${root_dev##*[a-z]}" ;;
    *) echo "SKIP: unexpected device name $root_dev"; exit 0 ;;
esac

if ! command -v growpart >/dev/null 2>&1; then
    echo "SKIP: growpart not installed (cloud-utils-growpart)"
    exit 0
fi
growpart "$disk" "$part" || echo "growpart returned $? (partition may already fill the disk)"

fs_type=$(findmnt -no FSTYPE / 2>/dev/null || true)
case "$fs_type" in
    xfs) xfs_growfs / || echo "xfs_growfs returned $?" ;;
    "")  echo "SKIP: unknown filesystem" ;;
    *)   resize2fs "$root_dev" || echo "resize2fs returned $?" ;;
esac

df -h /
"""


def grow_filesystem(session, log) -> bool:
    """Guest 内扩分区 + 文件系统。返回是否有实质动作（True）还是跳过（False）。

    任何失败都只写日志——到这里磁盘已经扩完了，Guest 侧失败不该把
    整个任务打成失败，用户可以之后手动执行。
    """
    try:
        output = session.capture(_GROW_SCRIPT, timeout=180, check=False)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log(f"Guest-side filesystem growth failed (disk itself is resized): {exc}")
        return False

    text = (output or "").strip()
    if text:
        for line in text.splitlines()[-10:]:
            log(f"  {line}")
    return "SKIP" not in text and bool(text)
