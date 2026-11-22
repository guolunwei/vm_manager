"""``vmrun`` 的薄封装。

把原来 ``vmctl.sh`` 里零散的 vmrun 调用集中到这里，统一处理编码、超时、
平台差异和报错。所有上层服务只通过本模块与 VMware 交互。

踩过的坑（改代码前请先读）：

1. **Windows 版 vmrun 会把以 ``/`` 开头的参数当成选项**。
   所以 ``runScriptInGuest`` 的解释器必须写 ``bin/bash``，不能写 ``/bin/bash``，
   否则报 ``A file was not found``。见 :data:`SCRIPT_INTERPRETER`。

2. **``checkToolsState`` 会滞后**。Guest 里已经能执行命令了，它仍可能长时间
   报 ``installed``，甚至一直不更新。因此「Guest 是否就绪」必须用「真的跑一条
   命令」来判定，``checkToolsState`` 只当加速路径。见 :func:`wait_guest_ready`。

3. **Windows 上必须加 ``CREATE_NO_WINDOW``**，否则每次调用都会闪一个黑框；
   以服务方式运行时还可能因为没有控制台而卡住。

4. **输出编码不一定是 UTF-8**。中文 Windows 下 stderr 可能是 GBK。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from django.conf import settings

# Windows 版 vmrun 会把 "/xxx" 当成命令行选项，必须去掉前导斜杠
SCRIPT_INTERPRETER = "bin/bash"

#: 认为 Guest 已经能执行命令的探针脚本
READY_PROBE = "true"


class VmrunError(RuntimeError):
    """vmrun 调用失败。``output`` 保留原始输出方便排查。"""

    def __init__(self, message: str, output: str = "", code: int | None = None):
        super().__init__(message)
        self.output = output or message
        self.code = code


# ---------------------------------------------------------------------------
# 可执行文件与平台
# ---------------------------------------------------------------------------
def vmrun_exe() -> str:
    configured = (settings.VMM.get("VMRUN") or "").strip()
    if not configured:
        configured = "vmrun.exe" if os.name == "nt" else "vmrun"
    # 配置成裸命令名时交给 PATH 解析，先确认存在，便于给出可读的报错
    if os.path.sep not in configured and "/" not in configured:
        found = shutil.which(configured)
        if found:
            return found
    return configured


def host_type() -> str:
    """``-T`` 参数：Workstation / Player 用 ws，Fusion 用 fusion。"""
    exe = vmrun_exe().lower()
    if "fusion" in exe or "vmware fusion" in exe:
        return "fusion"
    if "player" in exe:
        return "player"
    return "ws"


def is_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# 底层调用
# ---------------------------------------------------------------------------
def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _creation_flags() -> int:
    if is_windows():
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def run(
    args: list[str],
    *,
    timeout: int = 120,
    guest: tuple[str, str] | None = None,
) -> tuple[int, str]:
    """执行一条 vmrun 命令，返回 ``(returncode, 输出)``。

    ``guest=(user, password)`` 用于在客户机内执行命令的场景。
    """
    cmd = [vmrun_exe(), "-T", host_type()]
    if guest:
        user, password = guest
        cmd += ["-gu", user, "-gp", password]
    cmd += [str(a) for a in args]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise VmrunError(
            f"vmrun not found: {vmrun_exe()}. Install VMware Workstation/Player/Fusion, "
            "or set VMM_VMRUN in config.ini to the executable path."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VmrunError(f"vmrun timed out ({timeout}s): {' '.join(map(str, args[:2]))}") from exc
    except OSError as exc:  # 权限、路径过长等
        raise VmrunError(f"Failed to invoke vmrun: {exc}") from exc

    output = (_decode(proc.stdout) + _decode(proc.stderr)).strip()
    return proc.returncode, output


def run_ok(args: list[str], *, timeout: int = 120, guest: tuple[str, str] | None = None) -> str:
    """执行并断言成功。"""
    code, output = run(args, timeout=timeout, guest=guest)
    if code != 0:
        raise VmrunError(output or f"vmrun exited with code {code}", output=output, code=code)
    return output


def normpath(path: str) -> str:
    """路径比较用：Windows 下统一大小写和分隔符。"""
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path))


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def list_running(*, timeout: int = 60) -> list[str]:
    """返回正在运行的 vmx 绝对路径（已规范化）。"""
    code, output = run(["list"], timeout=timeout)
    if code != 0:
        raise VmrunError(output or "Failed to get the running VM list", output=output, code=code)
    result = []
    for line in output.splitlines():
        line = line.strip()
        # 第一行固定是 "Total running VMs: N"
        if not line or line.lower().startswith("total running"):
            continue
        result.append(normpath(line))
    return result


def list_running_set() -> set[str]:
    return set(list_running())


def is_running(vmx: str) -> bool:
    try:
        return normpath(vmx) in list_running_set()
    except VmrunError:
        return False


def tools_state(vmx: str, *, timeout: int = 60) -> str:
    """``running`` / ``installed`` / ``notInstalled`` / ``unknown``。

    注意它可能滞后，不要单独用它判断 Guest 是否可用。
    """
    try:
        code, output = run(["checkToolsState", vmx], timeout=timeout)
    except VmrunError:
        return "unknown"
    if code != 0 or not output:
        return "unknown"
    return output.strip().splitlines()[-1].strip()


def get_guest_ip(
    vmx: str,
    *,
    wait: bool = False,
    timeout: int = 60,
) -> str | None:
    """取 Guest 主 IP。Tools 没起来或拿不到时返回 None。"""
    args = ["getGuestIPAddress", vmx]
    if wait:
        args.append("-wait")
    try:
        code, output = run(args, timeout=timeout)
    except VmrunError:
        return None
    if code != 0:
        return None
    for line in output.splitlines():
        candidate = line.strip()
        if candidate and _looks_like_ip(candidate):
            return candidate
    return None


def _looks_like_ip(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
def clone(
    src_vmx: str,
    dst_vmx: str,
    *,
    snapshot: str = "",
    clone_name: str = "",
    linked: bool = True,
    timeout: int = 900,
) -> str:
    """克隆虚拟机。

    ``linked=True`` 是链接克隆（省磁盘、依赖母机）；``False`` 是完整克隆。
    """
    if os.path.exists(dst_vmx):
        raise VmrunError(f"Target already exists: {dst_vmx}")
    Path(dst_vmx).parent.mkdir(parents=True, exist_ok=True)

    args = ["clone", src_vmx, dst_vmx, "linked" if linked else "full"]
    if snapshot:
        args.append(f"-snapshot={snapshot}")
    if clone_name:
        args.append(f"-cloneName={clone_name}")
    return run_ok(args, timeout=timeout)


def start(vmx: str, *, gui: bool = False, timeout: int = 240) -> str:
    return run_ok(["start", vmx, "gui" if gui else "nogui"], timeout=timeout)


def stop(vmx: str, *, hard: bool = False, timeout: int = 240) -> str:
    """关机。软关机会在失败时自动退回硬关机，避免网页上一操作就报错。"""
    if hard:
        return run_ok(["stop", vmx, "hard"], timeout=timeout)

    code, output = run(["stop", vmx, "soft"], timeout=timeout)
    if code == 0:
        return output
    # Tools 没装 / 已卡死时 soft 会失败，退回 hard
    code2, output2 = run(["stop", vmx, "hard"], timeout=timeout)
    if code2 == 0:
        return output2
    raise VmrunError(output or "Power-off failed", output=output, code=code)


def reset(vmx: str, *, timeout: int = 240) -> str:
    return run_ok(["reset", vmx, "soft"], timeout=timeout)


def suspend(vmx: str, *, hard: bool = False, timeout: int = 240) -> str:
    return run_ok(["suspend", vmx, "hard" if hard else "soft"], timeout=timeout)


def pause(vmx: str, *, timeout: int = 60) -> str:
    return run_ok(["pause", vmx], timeout=timeout)


def unpause(vmx: str, *, timeout: int = 60) -> str:
    return run_ok(["unpause", vmx], timeout=timeout)


def delete(vmx: str, *, timeout: int = 180) -> str:
    return run_ok(["deleteVM", vmx], timeout=timeout)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def list_snapshots(vmx: str, *, timeout: int = 60) -> list[str]:
    try:
        code, output = run(["listSnapshots", vmx], timeout=timeout)
    except VmrunError:
        return []
    if code != 0:
        return []
    names = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("total snapshots"):
            continue
        names.append(line)
    return names


def create_snapshot(vmx: str, name: str, *, timeout: int = 300) -> str:
    return run_ok(["snapshot", vmx, name], timeout=timeout)


def revert_snapshot(vmx: str, name: str, *, timeout: int = 300) -> str:
    return run_ok(["revertToSnapshot", vmx, name], timeout=timeout)


def delete_snapshot(vmx: str, name: str, *, timeout: int = 300) -> str:
    return run_ok(["deleteSnapshot", vmx, name], timeout=timeout)


# ---------------------------------------------------------------------------
# Guest 内执行
# ---------------------------------------------------------------------------
def run_script(
    vmx: str,
    script: str,
    guest: tuple[str, str],
    *,
    timeout: int = 120,
) -> str:
    """在 Guest 里跑一段 bash，**不取回脚本自身的输出**。

    解释器必须写 ``bin/bash``（不带前导斜杠），原因见模块顶部「坑 1」。

    ``vmrun runScriptInGuest`` 不回传脚本的 stdout，所以本函数只适合「执行动作」；
    要读结果请用 :func:`run_script_capture`。
    """
    return run_ok(
        ["runScriptInGuest", vmx, SCRIPT_INTERPRETER, script],
        timeout=timeout,
        guest=guest,
    )


#: 客户机内落盘临时输出用的文件名前缀
_CAPTURE_PREFIX = ".vmm-out-"


def run_script_capture(
    vmx: str,
    script: str,
    guest: tuple[str, str],
    *,
    timeout: int = 120,
) -> tuple[int, str]:
    """在 Guest 里跑 bash 并取回它的 ``(退出码, 输出)``。

    坑：``runScriptInGuest`` 是「发出去就不管」的，脚本的 stdout/stderr 不会
    回到宿主机。想拿到结果只能绕一圈 —— 让脚本把输出重定向到客户机里的临时
    文件，再用 ``copyFileFromGuestToHost`` 拷回来。代价是每条查询多两次 vmrun
    调用，所以只给「读信息」用，执行动作仍然走 :func:`run_script`。
    """
    import tempfile
    import uuid

    token = uuid.uuid4().hex[:12]
    guest_out = f"/tmp/{_CAPTURE_PREFIX}{token}"

    # 用子壳包住脚本：脚本里的 exit 不会跳过收尾的退出码写入
    wrapped = f'( {script} ) > {guest_out} 2>&1; echo "__VMM_RC__:$?" >> {guest_out}'
    run_script(vmx, wrapped, guest, timeout=timeout)

    local_dir = tempfile.mkdtemp(prefix="vmm-cap-")
    local_out = os.path.join(local_dir, "out.txt")
    try:
        run_ok(
            ["copyFileFromGuestToHost", vmx, guest_out, local_out],
            timeout=timeout,
            guest=guest,
        )
        with open(local_out, "rb") as fh:
            text = _decode(fh.read())
    except (VmrunError, OSError):
        return 1, ""
    finally:
        shutil.rmtree(local_dir, ignore_errors=True)
        try:  # 客户机里的临时文件顺手删掉，失败不影响结果
            run_ok(["deleteFileInGuest", vmx, guest_out], timeout=30, guest=guest)
        except VmrunError:
            pass

    code = 0
    lines = text.splitlines()
    if lines and lines[-1].startswith("__VMM_RC__:"):
        raw = lines[-1].split(":", 1)[1].strip()
        code = int(raw) if raw.lstrip("-").isdigit() else 0
        lines = lines[:-1]
    return code, "\n".join(lines)


def run_program(
    vmx: str,
    program: str,
    args: list[str],
    guest: tuple[str, str],
    *,
    timeout: int = 120,
) -> str:
    """在 Guest 里跑一个可执行文件，并用退出码表示成功与否。

    不加 ``-noWait``，保持阻塞才能拿到进程退出码（例如用 ``ping`` 的退出码
    判断网络是否可达）。
    """
    cmd = ["runProgramInGuest", vmx, program, *[str(a) for a in args]]
    return run_ok(cmd, timeout=timeout, guest=guest)


def probe_guest(vmx: str, guest: tuple[str, str], *, timeout: int = 25) -> bool:
    """Guest 是否已经能执行命令 —— 权威探针。"""
    try:
        run_script(vmx, READY_PROBE, guest, timeout=timeout)
        return True
    except VmrunError:
        return False


def wait_guest_ready(
    vmx: str,
    guest: tuple[str, str],
    *,
    timeout: int = 360,
    poll: float = 6.0,
    on_tick=None,
) -> bool:
    """等待 Guest 可执行命令。

    用「真跑一条 ``true``」判定，``checkToolsState`` 只当加速路径（坑 2）。
    """
    started = time.time()
    deadline = started + timeout
    last_report = 0.0

    while time.time() < deadline:
        if tools_state(vmx) == "running":
            return True
        if probe_guest(vmx, guest):
            return True

        elapsed = time.time() - started
        if on_tick and elapsed - last_report >= 30:
            last_report = elapsed
            on_tick(int(elapsed))
        time.sleep(poll)
    return False


def wait_power_state(
    vmx: str,
    target: str,
    *,
    timeout: int = 180,
    poll: float = 2.0,
) -> bool:
    """等电源状态变成期望值。

    ``vmrun start`` 返回 0 只代表命令被接受，不代表已经开机完成。
    """
    deadline = time.time() + timeout
    wanted_running = target == "running"
    while time.time() < deadline:
        if is_running(vmx) == wanted_running:
            return True
        time.sleep(poll)
    return False
