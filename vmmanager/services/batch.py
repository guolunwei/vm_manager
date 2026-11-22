"""批量创建：一次提交多台虚拟机。

设计要点：

1. **逐行独立校验**。一行填错不该让整批失败，所以校验错误按行返回，
   通过的行照常入队，前端能逐行标红。
2. **入队而不是等待**。每台仍是独立的 ``provision`` 任务，由 worker 串行消费，
   页面提交后立刻返回任务列表。这样批量 20 台也不会把 HTTP 请求挂住。
3. **重名判断要比数据库更严格**。除了库里已有的机器，还要拦住本次提交内部
   自己重名的行——否则第一台入队成功、第二台在写入时才发现目录已存在。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError

from vmmanager.jobs import queue
from vmmanager.models import (
    CloneType,
    IpLease,
    Job,
    JobKind,
    VirtualMachine,
    VmTemplate,
    vm_name_validator,
)

# 一次提交的行数上限。20 台 × 约 30 秒 ≈ 10 分钟，再多人就该分批了。
MAX_ROWS = 20

# 批量动作 → （任务类型，是否强制）。value 里 force=True 表示下发硬操作。
BULK_ACTIONS: dict[str, tuple[str, bool]] = {
    "start": (JobKind.POWER_ON, False),
    "stop": (JobKind.POWER_OFF, False),
    "hard-stop": (JobKind.POWER_OFF, True),
    "reset": (JobKind.POWER_RESET, False),
    "suspend": (JobKind.SUSPEND, False),
    "delete": (JobKind.DELETE, False),
}


def bulk_action(
    action: str,
    ids: list[int],
    *,
    user,
) -> dict:
    """对一组虚拟机批量入队同一动作。

    与批量创建同一哲学：**逐台独立入队**，一台失败（重名任务冲突、机器已不存在）
    不影响其余，调用方拿到 ``jobs`` / ``errors`` 明细自行汇总。
    权限（删除仅限管理员）由调用方检查——Web 与 API 的权限语义不同。
    去重键与单台动作一致（``{kind}:{vm.pk}``），所以批量提交后立刻补点单台
    也不会产生重复任务。
    """
    mapping = BULK_ACTIONS.get(action)
    if mapping is None:
        return {"jobs": [], "errors": [{"id": 0, "message": f"未知操作：{action}"}]}
    kind, force = mapping

    jobs: list[dict] = []
    errors: list[dict] = []

    for vm_id in ids:
        vm = VirtualMachine.objects.filter(pk=vm_id).first()
        if vm is None:
            errors.append({"id": vm_id, "name": "", "message": "虚拟机不存在或已被删除"})
            continue

        payload: dict = {"vm_id": vm.pk}
        if force:
            payload["force"] = True
        if kind == JobKind.DELETE:
            payload["remove_files"] = True

        try:
            job = queue.enqueue(
                kind,
                payload=payload,
                vm=vm,
                user=user,
                dedupe_key=f"{kind}:{vm.pk}",
            )
        except queue.DuplicateJob as exc:
            errors.append({"id": vm.pk, "name": vm.name, "message": str(exc)})
            continue
        except queue.QueueFull as exc:
            errors.append({"id": vm.pk, "name": vm.name, "message": str(exc)})
            continue

        jobs.append({"id": vm.pk, "name": vm.name, "job_id": job.pk})

    return {"jobs": jobs, "errors": errors}


@dataclass
class Row:
    """一行通过校验的创建请求。

    ``template_id`` / ``clone_type`` 为行级覆盖：None 时回落到公共配置。
    """

    index: int
    name: str
    cpus: int
    memory_mb: int
    ip: str = ""
    template_id: int | None = None
    clone_type: str = ""


@dataclass
class ValidationOutcome:
    rows: list[Row] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _row_error(index: int, name: str, message: str) -> dict:
    return {"index": index, "name": name, "message": message}


def validate_rows(
    raw_rows: list[dict],
    *,
    default_cpus: int,
    default_memory_mb: int,
    template_ids: set[int] | None = None,
) -> ValidationOutcome:
    """把前端传来的行数据校验成 ``Row`` 列表，错误按行收集。

    ``template_ids`` 是可选的合法模板主键集合；传了就逐行校验
    ``template_id`` 覆盖值是否有效，没传则原样保留。
    """
    outcome = ValidationOutcome()

    if not raw_rows:
        outcome.errors.append(_row_error(0, "", "至少需要一台虚拟机"))
        return outcome

    if len(raw_rows) > MAX_ROWS:
        outcome.errors.append(_row_error(0, "", f"单次最多 {MAX_ROWS} 台，请分批提交"))
        return outcome

    seen_names: set[str] = set()
    seen_ips: set[str] = set()

    for index, item in enumerate(raw_rows):
        name = str(item.get("name") or "").strip()
        if not name:
            outcome.errors.append(_row_error(index + 1, "", "名称必填"))
            continue

        try:
            vm_name_validator(name)
        except ValidationError as exc:
            outcome.errors.append(_row_error(index + 1, name, str(exc.messages[0])))
            continue

        lowered = name.lower()
        if lowered in seen_names:
            outcome.errors.append(_row_error(index + 1, name, "本批次中名称重复"))
            continue
        if VirtualMachine.objects.filter(name__iexact=name).exists():
            outcome.errors.append(_row_error(index + 1, name, "已存在同名虚拟机"))
            continue

        cpus = _as_int(item.get("cpus"), default_cpus)
        if not 1 <= cpus <= 128:
            outcome.errors.append(_row_error(index + 1, name, "vCPU 必须在 1 到 128 之间"))
            continue

        memory_mb = _as_int(item.get("memory_mb"), default_memory_mb)
        if not 256 <= memory_mb <= 1048576:
            outcome.errors.append(_row_error(index + 1, name, "内存必须在 256 到 1048576 MB 之间"))
            continue

        ip = str(item.get("ip") or "").strip()
        if ip:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                outcome.errors.append(_row_error(index + 1, name, "IP 格式无效"))
                continue
            if ip in seen_ips:
                outcome.errors.append(_row_error(index + 1, name, "本批次中 IP 重复"))
                continue
            if IpLease.objects.filter(ip=ip).exists():
                outcome.errors.append(_row_error(index + 1, name, "IP 已被占用"))
                continue
            seen_ips.add(ip)

        # 行级覆盖：模板与克隆方式。留空/非法都回落到公共配置。
        row_template_id = _as_optional_int(item.get("template_id"))
        if row_template_id is not None and template_ids is not None and row_template_id not in template_ids:
            outcome.errors.append(_row_error(index + 1, name, "该行选择的模板不存在或已停用"))
            continue

        row_clone = str(item.get("clone_type") or "").strip()
        if row_clone not in (CloneType.LINKED, CloneType.FULL):
            row_clone = ""

        seen_names.add(lowered)
        outcome.rows.append(
            Row(
                index=index + 1,
                name=name,
                cpus=cpus,
                memory_mb=memory_mb,
                ip=ip,
                template_id=row_template_id,
                clone_type=row_clone,
            )
        )

    return outcome


def _as_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _as_optional_int(value) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def submit(
    rows: list[Row],
    *,
    template: VmTemplate,
    user,
    clone_type: str = CloneType.LINKED,
    deploy_ssh_key: bool = True,
    guest_password: str = "",
    notes: str = "",
    templates: dict[int, VmTemplate] | None = None,
) -> dict:
    """逐行入队，返回成功与失败明细。

    这里不预先分配 IP：自动分配要交给 ``provision`` 在真正创建时做，
    否则同时入队的一批机器会拿到同一批「此刻还空着」的地址。

    ``templates`` 是模板主键到实例的映射，行里带了 ``template_id`` 就用行级
    模板；克隆方式同理，行的 ``clone_type`` 无效时回落到公共配置。
    """
    jobs: list[dict] = []
    errors: list[dict] = []
    template_map = templates or {}

    for row in rows:
        row_template = template_map.get(row.template_id, template)
        row_clone = row.clone_type if row.clone_type in (CloneType.LINKED, CloneType.FULL) else clone_type
        payload = {
            "name": row.name,
            "template_id": row_template.pk,
            "flavor_id": None,
            "cpus": row.cpus,
            "memory_mb": row.memory_mb,
            "ip": row.ip,
            "clone_type": row_clone,
            "deploy_ssh_key": bool(deploy_ssh_key),
            "guest_password": guest_password or "",
            "notes": notes or "",
        }
        try:
            job: Job = queue.enqueue(
                JobKind.PROVISION,
                payload=payload,
                label=row.name,
                user=user,
                dedupe_key=f"provision:{row.name.lower()}",
            )
        except queue.DuplicateJob as exc:
            errors.append(_row_error(row.index, row.name, str(exc)))
            continue
        except queue.QueueFull as exc:
            errors.append(_row_error(row.index, row.name, str(exc)))
            continue

        jobs.append({"index": row.index, "name": row.name, "job_id": job.pk})

    return {"jobs": jobs, "errors": errors}
