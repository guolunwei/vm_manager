"""noVNC 控制台的底层支撑。

VMware Workstation 自带 VNC server：往 vmx 写三个键
（RemoteDisplay.vnc.enabled / port / password），下次开机就会监听对应端口。
noVNC 是纯浏览器端 RFB 客户端，中间需要 websockify 把 WebSocket 桥到 TCP。

链路：浏览器 noVNC --ws--> websockify(:6080) --tcp--> VMware VNC(:59xx)
websockify 用 token 目录插件路由：Django 签发 token 文件（内容 host:port），
浏览器连 ws://host:6080/<token>，代理按 token 找目标。

坑（实测得出）：
- vmx 里的 VNC 配置在虚拟机**开机时**才被 VMware 读取，运行中改 vmx 不生效，
  必须下次开机（或重启）才监听端口。
- 密码最长 8 个字符，超长会被截断，干脆只生成 8 位。
- 端口分配要跨所有 vmx 去重——VMware 对重复端口的行为是后启动的失败。
"""

import re
import secrets
import socket
import string
import time
from pathlib import Path

from django.conf import settings

from . import vmx

VNC_PREFIX = "RemoteDisplay.vnc"
TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def config_for(vmx_path: str | Path) -> dict:
    """读某个 vmx 当前的 VNC 配置（原样返回 vmx 里的值）。"""
    data = vmx.read(vmx_path)
    enabled = str(data.get(f"{VNC_PREFIX}.enabled", "FALSE")).strip('"').upper() == "TRUE"
    port_raw = str(data.get(f"{VNC_PREFIX}.port", "")).strip('"')
    password = str(data.get(f"{VNC_PREFIX}.password", "")).strip('"')
    port = int(port_raw) if port_raw.isdigit() else None
    return {"enabled": enabled, "port": port, "password": password}


def generate_password(length: int = 8) -> str:
    """VNC 密码：8 位字母数字（VMware 上限 8 字符）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _ports_in_use_elsewhere(vmx_path: str | Path) -> set[int]:
    """收集其它 vmx 已占用的 VNC 端口，避免分配冲突。"""
    base = Path(settings.VMM["BASE_DIR"])
    used: set[int] = set()
    if not base.exists():
        return used
    resolved = Path(vmx_path).resolve()
    for candidate in sorted(base.rglob("*.vmx")):
        if candidate.resolve() == resolved:
            continue
        cfg = config_for(candidate)
        if cfg["port"]:
            used.add(cfg["port"])
    return used


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def ensure_config(vm, *, log=None) -> dict:
    """保证 vmx 里有一套可用的 VNC 配置，返回 {"config": ..., "changed": bool}。

    端口从 5900 + pk 起找空闲；密码缺失时生成并写回。只改 vmx 文件，
    对运行中的虚拟机不生效——调用方负责提示「下次开机生效」。
    """
    cfg = config_for(vm.vmx_path)
    changed = False
    patch: dict[str, str] = {}

    if not cfg["enabled"]:
        patch[f"{VNC_PREFIX}.enabled"] = "TRUE"
        cfg["enabled"] = True
        changed = True

    if not cfg["port"]:
        used = _ports_in_use_elsewhere(vm.vmx_path)
        port = 5900 + (vm.pk % 90)
        while port in used or not _port_free(port):
            port += 1
        patch[f"{VNC_PREFIX}.port"] = str(port)
        cfg["port"] = port
        changed = True

    if not cfg["password"]:
        patch[f"{VNC_PREFIX}.password"] = generate_password()
        cfg["password"] = patch[f"{VNC_PREFIX}.password"]
        changed = True

    if patch:
        vmx.patch(vm.vmx_path, patch)
        if log:
            log(f"VNC config written to vmx (port {cfg['port']})")

    return {"config": cfg, "changed": changed}


def is_listening(port: int) -> bool:
    """VMware 是否已在这个端口监听 VNC（运行中写入的配置要下次开机才生效）。"""
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def issue_token(vmx_path: str | Path, *, ttl: int | None = None) -> str:
    """签发一次性代理 token：写 token 文件供 websockify 的 TokenFile 插件查找。

    文件内容 `token: 127.0.0.1:{port}`；顺带清理过期 token。
    """
    cfg = config_for(vmx_path)
    if not cfg["enabled"] or not cfg["port"]:
        raise RuntimeError("VNC is not enabled on this VM")
    if ttl is None:
        ttl = settings.VMM["VNC_TOKEN_TTL"]

    token_dir = Path(settings.DATA_DIR) / "vnc_tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    # 过期清理：mtime 超过 TTL 的 token 一律删掉
    now = time.time()
    for stale in token_dir.glob("*"):
        try:
            if now - stale.stat().st_mtime > ttl:
                stale.unlink(missing_ok=True)
        except OSError:
            pass

    token = secrets.token_hex(16)
    (token_dir / f"{token}.tok").write_text(
        f"{token}: 127.0.0.1:{cfg['port']}\n",
        encoding="ascii",
    )
    return token


def validate_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token or ""))
