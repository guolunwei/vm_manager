"""``.vmx`` 配置文件的读写。

vmx 是 ``key = "value"`` 文本。改写时**保持原有行序和注释**，只替换目标键的值、
缺失的键追加到末尾 —— 这样用户用 VMware 图形界面打开时不会发现配置被打乱。

**踩过的三个坑，按踩到的顺序记在这里：**

1. **键名大小写不敏感**。VMware 自己的 vmx 里写的是 ``memsize``（全小写），
   而文档和大多数示例写 ``memSize``。如果按大小写敏感去匹配，``memSize`` 匹配
   不到已有的 ``memsize``，于是被当成新键**追加到文件末尾** —— 同一个参数出现
   两次且取值不同，VMware 直接报 ``Cannot read the virtual machine configuration
   file`` 拒绝开机。所以这里统一按小写索引，改写时**沿用文件里原有的键名写法**。

2. **编码要跟着声明走**。vmx 内部带一行 ``.encoding = "GBK"`` 声明自己的编码，
   中文 Windows 下常见为 GBK。按 UTF-8 读写而声明还是 GBK，同样会被拒绝读取。
   规则是：**声明什么编码就用什么编码读写**，没声明才自己猜。

3. **换行必须是 CRLF**。``Path.read_text()`` 会做通用换行转换，把 CRLF 悄悄变成
   LF；VMware 同样不认。所以读写都走 ``read_bytes`` / ``newline=""``。

另一条注意：多数硬件参数（memSize / numvcpus）只在虚拟机**关机**时生效，
运行中改写会被 VMware 忽略甚至覆盖。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_KEY_VALUE = re.compile(r"^(?P<key>[A-Za-z0-9._:-]+)\s*=\s*(?P<value>.*)$")
_ENCODING_DECL = re.compile(r'^\s*\.encoding\s*=\s*"([^"]+)"', re.M)

#: VMware 的编码名与 Python codec 名不完全一致，做几个映射
_ENCODING_ALIASES = {
    "gb2312": "gbk",
    "windows-1252": "cp1252",
    "ansi": "gbk",
}


class VmxError(RuntimeError):
    pass


class VmxDict(dict):
    """键名大小写不敏感的字典。

    vmx 的键在 VMware 那边是大小写不敏感的（模板里就写成 ``memsize``），
    所以读出来的字典也必须按同样规则查找，否则 ``data["memSize"]`` 取不到值。
    内部存储统一用小写键，迭代拿到的也是小写键。
    """

    @staticmethod
    def _norm(key):
        return key.lower() if isinstance(key, str) else key

    def __getitem__(self, key):
        return super().__getitem__(self._norm(key))

    def __contains__(self, key):
        return super().__contains__(self._norm(key))

    def get(self, key, default=None):
        return super().get(self._norm(key), default)

    def pop(self, key, *args):
        return super().pop(self._norm(key), *args)

    def setdefault(self, key, default=None):
        return super().setdefault(self._norm(key), default)


def detect_encoding(source) -> str:
    """找出 vmx 声明的编码；没声明就按 UTF-8 → GBK 试。

    ``source`` 可以是路径，也可以是已读出的 ``bytes``。
    """
    try:
        if isinstance(source, (bytes, bytearray)):
            raw = bytes(source)
        else:
            raw = Path(source).read_bytes()
    except OSError:
        return "utf-8"

    # .encoding 声明本身是 ASCII，用 latin-1 读头部不会出错
    head = raw[:4096].decode("latin-1", errors="replace")
    match = _ENCODING_DECL.search(head)
    if match:
        declared = match.group(1).strip().lower()
        declared = _ENCODING_ALIASES.get(declared, declared)
        try:
            "".encode(declared)
        except LookupError:
            return "utf-8"
        return declared

    # 没有声明：能按 UTF-8 解码就 UTF-8，否则按中文 Windows 的 GBK 处理
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gbk"


def _read_text(vmx_path: str) -> tuple[str, str]:
    """返回 ``(原始文本, 编码)``。

    必须走 ``read_bytes`` 而不是 ``read_text``：后者会做通用换行转换，把
    CRLF 悄悄变成 LF。VMware 写出的 vmx 是 CRLF，换成 LF 后它会报
    ``Cannot read the virtual machine configuration file`` 拒绝开机。
    """
    path = Path(vmx_path)
    if not path.is_file():
        raise VmxError(f"vmx file not found: {vmx_path}")
    raw = path.read_bytes()
    encoding = detect_encoding(raw)
    return raw.decode(encoding, errors="replace"), encoding


def read(vmx_path: str) -> VmxDict:
    """读成字典。重复键取最后一次（与 VMware 行为一致）。键名大小写不敏感。"""
    text, _ = _read_text(vmx_path)
    data = VmxDict()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        match = _KEY_VALUE.match(line)
        if not match:
            continue
        data[match.group("key").lower()] = _unquote(match.group("value"))
    return data


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _format_value(value) -> str:
    if isinstance(value, bool):
        return '"TRUE"' if value else '"FALSE"'
    text = str(value)
    return f'"{text}"'


def patch(vmx_path: str, updates: dict[str, object]) -> list[str]:
    """原子改写指定键，返回实际被修改的键列表。

    保留原有顺序、注释与编码；新键追加到文件末尾。

    键名按大小写不敏感匹配，命中后**沿用文件里原来的写法**（模板写
    ``memsize`` 就继续写 ``memsize``），绝不因为调用方写成 ``memSize``
    而多追加一条 —— 那是会被 VMware 拒绝读配置的重复键。
    """
    path = Path(vmx_path)
    original, encoding = _read_text(vmx_path)

    # vmx 在 Windows 上通常是 CRLF，按原样拼回
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()

    pending = {k.lower(): (k, v) for k, v in updates.items() if v is not None}
    changed: list[str] = []

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        match = _KEY_VALUE.match(stripped)
        if not match:
            continue
        key = match.group("key")
        entry = pending.pop(key.lower(), None)
        if entry is None:
            continue
        new_line = f"{key} = {_format_value(entry[1])}"
        if stripped != new_line:
            lines[index] = new_line
            changed.append(key)

    for canonical, value in pending.values():
        lines.append(f"{canonical} = {_format_value(value)}")
        changed.append(canonical)

    if not changed:
        return []

    content = newline.join(lines) + newline
    _atomic_write(path, content, encoding)
    return changed


def _atomic_write(path: Path, content: str, encoding: str) -> None:
    """先写临时文件再替换，避免写一半断电导致 vmx 损坏。

    编码必须与文件里 ``.encoding`` 的声明一致，否则 VMware 会拒绝读取。
    """
    fd, tmp_name = tempfile.mkstemp(prefix=".vmx-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, errors="replace", newline="") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 常用字段
# ---------------------------------------------------------------------------
def cpu_count(vmx_path: str, default: int = 2) -> int:
    """取 vCPU 数。优先 numvcpus，其次 cpuid.coresPerSocket。"""
    data = read(vmx_path)
    for key in ("numvcpus", "cpuid.coresPerSocket"):
        raw = (data.get(key) or "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    return default


def memory_mb(vmx_path: str, default: int = 4096) -> int:
    data = read(vmx_path)
    try:
        return int(data.get("memSize", "0") or 0) or default
    except ValueError:
        return default


def cpu_topology(numvcpus: int) -> int:
    """给 ``numvcpus`` 算一个自洽的 ``cpuid.coresPerSocket``。

    vmx 里 vCPU 总数是 ``numvcpus``，「每路核数」是 ``cpuid.coresPerSocket``，
    路数 = numvcpus / coresPerSocket。模板里通常固定写着
    ``cpuid.coresPerSocket = "2"``，只改 ``numvcpus`` 会得到 1/2、3/2 这种
    除不尽的组合。

    这里统一按**单路**处理（``coresPerSocket == numvcpus``），理由：

    * 永远整除，不会构造出自相矛盾的配置；
    * 路数恒为 1，绕开客户机系统的「每路 CPU 数量」限制和按路授权的计价方式
      （部分 Windows 版本按 socket 授权）；
    * Workstation 允许每路最多 128 核，桌面场景下 ``numvcpus`` 远达不到。
    """
    return max(1, int(numvcpus))


def mac_address(vmx_path: str, nic: int = 0) -> str:
    """取 ``ethernet{nic}`` 的 MAC，格式统一成带冒号的大写。"""
    data = read(vmx_path)
    raw = (data.get(f"ethernet{nic}.generatedAddress") or data.get(f"ethernet{nic}.address") or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(cleaned) != 12:
        return raw.upper()
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2)).upper()


def display_name(vmx_path: str) -> str:
    return read(vmx_path).get("displayName", "") or Path(vmx_path).stem


def guest_os(vmx_path: str) -> str:
    return read(vmx_path).get("guestOS", "")


def disk_files(vmx_path: str) -> list[str]:
    """列出 vmx 引用的磁盘文件（相对路径按 vmx 所在目录解析）。

    ``read()`` 返回的键统一是小写（``scsi0:0.filename``），这里必须用
    小写正则匹配——曾写成 ``\\.fileName$``，永远匹配不到，磁盘列表一直是空的。
    """
    data = read(vmx_path)
    base = Path(vmx_path).parent
    result = []
    for key, value in data.items():
        if re.match(r"^(scsi|sata|ide|nvme)\d+:\d+\.filename$", key) and value:
            if value.lower().endswith(".vmdk"):
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = base / value
                result.append(str(candidate))
    return result


def summary(vmx_path: str) -> dict:
    """给同步/纳管用的一组基础信息。"""
    data = read(vmx_path)
    ethernets = sorted(
        {
            int(m.group(1))
            for key in data
            if (m := re.match(r"^ethernet(\d+)\.present$", key)) and data[key].upper() == "TRUE"
        }
    )
    return {
        "display_name": data.get("displayName") or Path(vmx_path).stem,
        "guest_os": data.get("guestOS", ""),
        "cpus": cpu_count(vmx_path),
        "memory_mb": memory_mb(vmx_path),
        "mac": mac_address(vmx_path, 0),
        "firmware": data.get("firmware", "bios"),
        "nics": [
            {
                "index": idx,
                "mac": mac_address(vmx_path, idx),
                "connection": data.get(f"ethernet{idx}.connectionType", ""),
            }
            for idx in ethernets
        ],
        "disks": disk_files(vmx_path),
        "tools_iso": data.get("toolsInstallManager.updateCounter", ""),
    }
