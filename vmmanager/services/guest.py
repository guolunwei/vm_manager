"""在客户机内部执行配置操作。

只做「创建后需要落地到系统里」的事：主机名、静态 IP、SSH 公钥、密码。
所有方法都通过 vmrun 的 ``runScriptInGuest`` 实现，因此要求：

* 虚拟机处于开机状态
* 客户机内已装 open-vm-tools（并在运行）
* 模板里有可用的管理员账号

对 Linux 走 shell + nmcli；Windows 只做了最基础的 netsh 分支。
"""

from __future__ import annotations

import getpass
import os
import shlex
from pathlib import Path

from . import vmrun
from .vmrun import VmrunError

#: nmcli 连接名探测脚本：优先有线、已激活的连接
_DETECT_CONN_SCRIPT = (
    "nmcli -t -f NAME,TYPE,DEVICE con show --active "
    '| awk -F: \'$2=="802-3-ethernet" && $3!="" {print $1; exit}\''
)


class GuestError(RuntimeError):
    pass


class GuestSession:
    """一台运行中的客户机会话。"""

    def __init__(
        self,
        vmx_path: str,
        user: str,
        password: str,
        *,
        os_family: str = "linux",
    ):
        self.vmx_path = vmx_path
        self.user = user
        self.password = password
        self.os_family = os_family or "linux"

    # -- 基础 --------------------------------------------------------------
    @property
    def creds(self) -> tuple[str, str]:
        return (self.user, self.password)

    @property
    def is_linux(self) -> bool:
        return self.os_family == "linux"

    def run(self, script: str, *, timeout: int = 120, check: bool = True) -> str:
        """在客户机里执行脚本（只执行，不取输出）。

        ``runScriptInGuest`` 不回传 stdout，所以这里返回值不含脚本输出。
        需要读结果请用 :meth:`capture`。
        """
        if not self.is_linux:
            raise GuestError("Only shell scripts in Linux guests are currently supported")
        try:
            return vmrun.run_script(self.vmx_path, script, self.creds, timeout=timeout)
        except VmrunError as exc:
            if check:
                raise GuestError(f"Failed to execute in guest: {exc}") from exc
            return str(exc)

    def capture(self, script: str, *, timeout: int = 120, check: bool = True) -> str:
        """在客户机里执行脚本并返回它的 stdout + stderr。

        比 :meth:`run` 多两次 vmrun 往返，只用于「读信息」。
        """
        if not self.is_linux:
            raise GuestError("Only shell scripts in Linux guests are currently supported")
        code, output = vmrun.run_script_capture(
            self.vmx_path,
            script,
            self.creds,
            timeout=timeout,
        )
        if code != 0 and check:
            raise GuestError(f"Failed to execute in guest (exit code {code}): {output.strip()}")
        return output

    def wait_ready(self, *, timeout: int = 360, on_tick=None) -> bool:
        return vmrun.wait_guest_ready(
            self.vmx_path,
            self.creds,
            timeout=timeout,
            on_tick=on_tick,
        )

    # -- 信息 --------------------------------------------------------------
    def uname(self) -> str:
        try:
            return self.capture("uname -srm", timeout=30).strip()
        except GuestError:
            return ""

    def detect_connection(self, preferred: str = "") -> str:
        """找出用于配置 IP 的 NetworkManager 连接名。

        CentOS 7 上连接名常常就是 ``eth0``，但也可能是 ``ens33`` 或
        ``Wired connection 1``，所以不能写死。
        """
        if preferred:
            quoted = shlex.quote(preferred)
            try:
                out = self.capture(
                    f"nmcli -t -f NAME con show | grep -Fx {quoted} || true",
                    timeout=30,
                ).strip()
                if out:
                    return preferred
            except GuestError:
                pass
        try:
            detected = self.capture(_DETECT_CONN_SCRIPT, timeout=30).strip().splitlines()
        except GuestError:
            detected = []
        for line in detected:
            name = line.strip()
            if name:
                return name
        return preferred or "eth0"

    def current_ipv4(self) -> str:
        try:
            out = self.capture(
                "ip -4 -o addr show scope global | awk '{print $4}' | head -1",
                timeout=30,
            )
            return out.strip().split("/")[0] if out.strip() else ""
        except GuestError:
            return ""

    # -- 配置 --------------------------------------------------------------
    def set_hostname(self, hostname: str) -> None:
        host = shlex.quote(hostname)
        self.run(
            f"hostnamectl set-hostname {host} 2>/dev/null || hostname {host}; "
            f"grep -q ' {hostname}$' /etc/hosts || echo '127.0.1.1 {hostname}' >> /etc/hosts",
            timeout=60,
            check=False,
        )

    def set_static_ip(
        self,
        ip: str,
        *,
        prefix: int = 24,
        gateway: str = "",
        connection: str = "",
        dns: str = "",
    ) -> None:
        """用 nmcli 配置静态 IP。

        切 IP 的瞬间连接会断，``nmcli con up`` 经常返回非 0，这是预期行为，
        这里不抛错，由调用方用 ping 校验最终结果。
        """
        conn = self.detect_connection(connection)
        quoted = shlex.quote(conn)

        parts = [
            f"nmcli connection modify {quoted} ipv4.method manual",
            f"ipv4.addresses {ip}/{prefix}",
        ]
        if gateway:
            parts.append(f"ipv4.gateway {shlex.quote(gateway)}")
        if dns:
            parts.append(f"ipv4.dns {shlex.quote(dns)}")
        parts.append("connection.autoconnect yes")

        script = (
            "set -x; "
            + " ".join(parts)
            + f"; nmcli connection up {quoted} || nmcli connection reload || true"
        )
        self.run(script, timeout=180, check=False)

    def set_dhcp(self, connection: str = "") -> None:
        conn = self.detect_connection(connection)
        quoted = shlex.quote(conn)
        self.run(
            f"nmcli connection modify {quoted} ipv4.method auto; nmcli connection up {quoted} || true",
            timeout=180,
            check=False,
        )

    def install_ssh_key(self, public_key: str, *, target_user: str = "") -> None:
        key = public_key.strip()
        if not key:
            raise GuestError("SSH public key content is empty")
        user = target_user or self.user
        quoted_key = shlex.quote(key)
        home = "/root" if user == "root" else f"/home/{user}"
        script = (
            f"install -d -m 700 {home}/.ssh && "
            f"touch {home}/.ssh/authorized_keys && "
            f"chmod 600 {home}/.ssh/authorized_keys && "
            f"grep -qxF {quoted_key} {home}/.ssh/authorized_keys || "
            f"echo {quoted_key} >> {home}/.ssh/authorized_keys"
        )
        self.run(script, timeout=60)
        if user != "root":
            self.run(
                f"chown -R {shlex.quote(user)}:{shlex.quote(user)} {home}/.ssh 2>/dev/null || true",
                timeout=30,
                check=False,
            )

    def set_password(self, new_password: str, *, target_user: str = "") -> None:
        user = shlex.quote(target_user or self.user)
        # chpasswd 走 stdin 更安全，避免密码出现在进程列表里
        self.run(
            f"printf '%s:%s\\n' {user} {shlex.quote(new_password)} | chpasswd",
            timeout=60,
        )

    def reboot(self) -> None:
        self.run("nohup shutdown -r +0 >/dev/null 2>&1 &", timeout=20, check=False)

    # -- 连通性 ------------------------------------------------------------
    def can_ping(self, target: str, *, count: int = 2) -> bool:
        """在客户机内部 ping 目标，用退出码判断。"""
        if not self.is_linux:
            return False
        try:
            vmrun.run_program(
                self.vmx_path,
                "bin/ping",
                ["-c", str(count), "-W", "2", target],
                self.creds,
                timeout=30,
            )
            return True
        except VmrunError:
            return False

    def can_reach(self, host: str, port: int = 22) -> bool:
        """在客户机内用 bash 的 /dev/tcp 探测端口。"""
        if not self.is_linux:
            return False
        script = f"timeout 3 bash -c '</dev/tcp/{host}/{port}'"
        try:
            self.run(script, timeout=20)
            return True
        except GuestError:
            return False


# ---------------------------------------------------------------------------
# SSH 公钥
# ---------------------------------------------------------------------------
def find_public_key() -> str:
    """找本机公钥，找不到就生成一对 ed25519。

    优先用配置里显式指定的公钥（``config.ini`` 的 ``SSH_PUBKEY``）：以服务方式
    运行时进程账号的家目录可能不是运维自己的，读 ``~/.ssh`` 会一无所获，
    显式配置最稳。没有配置才回退到本机密钥文件，再回退到现生成。
    """
    from django.conf import settings

    configured = (settings.VMM.get("SSH_PUBKEY") or "").strip()
    if configured:
        return configured

    home = Path(os.path.expanduser("~"))
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        candidate = home / ".ssh" / name
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue

    key_path = home / ".ssh" / "id_ed25519"
    try:
        import subprocess

        (home / ".ssh").mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-q", "-N", ""],
            capture_output=True,
            timeout=30,
            check=False,
        )
        pub = key_path.with_suffix(".pub")
        if pub.is_file():
            return pub.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def ssh_key_hint() -> str:
    """公钥找不到时给用户的下一步建议，避免只看到一句「跳过」。"""

    home = os.path.expanduser("~")
    return (
        f'No deployable SSH public key found. Set SSH_PUBKEY="ssh-ed25519 AAAA..." in config.ini, '
        f"or make sure the running process can read .pub files under {home}\\.ssh\\ "
        f"(current process account can read {home}: {os.access(home, os.R_OK)})"
    )


def default_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""
