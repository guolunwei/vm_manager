"""虚拟机与快照。"""

from __future__ import annotations

import os
import re

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from .base import TimeStampedModel

vm_name_validator = RegexValidator(
    regex=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"),
    message="名称只能包含字母、数字、点、下划线和连字符，且必须以字母或数字开头",
)

_INVALID_HOST_CHARS = re.compile(r"[^a-z0-9._-]")


def hostname_for(vm_name: str) -> str:
    """由虚拟机名推导主机名，与原 vmctl.sh 行为一致。"""
    host = vm_name.strip().lower().replace(" ", "-")
    return _INVALID_HOST_CHARS.sub("", host) or "vm"


class PowerState(models.TextChoices):
    RUNNING = "running", "运行中"
    STOPPED = "stopped", "已关机"
    SUSPENDED = "suspended", "已挂起"
    UNKNOWN = "unknown", "未知"


class CloneType(models.TextChoices):
    LINKED = "linked", "链接克隆（省磁盘，依赖模板）"
    FULL = "full", "完整克隆（独立磁盘）"
    EXTERNAL = "external", "外部导入（非本系统创建）"


class VirtualMachine(TimeStampedModel):
    """一台已纳管的虚拟机。"""

    name = models.CharField("名称", max_length=64, unique=True, validators=[vm_name_validator])
    hostname = models.CharField("主机名", max_length=64, blank=True, default="")
    vmx_path = models.CharField("vmx 路径", max_length=512, unique=True)

    template = models.ForeignKey(
        "VmTemplate",
        verbose_name="模板",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vms",
    )
    flavor = models.ForeignKey(
        "Flavor",
        verbose_name="规格",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vms",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_vms",
    )

    cpus = models.PositiveSmallIntegerField("vCPU", default=2)
    memory_mb = models.PositiveIntegerField("内存（MB）", default=4096)
    clone_type = models.CharField(
        "克隆方式",
        max_length=16,
        choices=CloneType.choices,
        default=CloneType.LINKED,
    )

    ip_address = models.GenericIPAddressField("IP 地址", null=True, blank=True, unique=True)
    mac_address = models.CharField("MAC 地址", max_length=32, blank=True, default="")
    power_state = models.CharField(
        "电源状态",
        max_length=16,
        choices=PowerState.choices,
        default=PowerState.UNKNOWN,
        db_index=True,
    )
    tools_state = models.CharField("Tools 状态", max_length=16, blank=True, default="")

    state_checked_at = models.DateTimeField("状态检查时间", null=True, blank=True)
    ip_checked_at = models.DateTimeField("IP 探测时间", null=True, blank=True)

    notes = models.TextField("备注", blank=True, default="")

    class Meta:
        verbose_name = "虚拟机"
        verbose_name_plural = "虚拟机"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["power_state", "-created_at"]),
            models.Index(fields=["ip_address"]),
        ]
        constraints = [
            # 名称与路径都按**不区分大小写**唯一：虚拟机的目录名直接来自名称，
            # 而 Windows 文件系统不区分大小写，vm-1 和 VM-1 会落到同一个目录里，
            # 光靠字段上的 unique=True（SQLite 默认区分大小写）挡不住。
            models.UniqueConstraint(
                Lower("name"),
                name="uniq_vm_name_ci",
            ),
            models.UniqueConstraint(
                Lower("vmx_path"),
                name="uniq_vm_vmx_path_ci",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    # -- 展示用 ------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self.power_state == PowerState.RUNNING

    @property
    def memory_display(self) -> str:
        return f"{self.memory_mb / 1024:g} GB"

    @property
    def spec_display(self) -> str:
        return f"{self.cpus}C / {self.memory_display}"

    @property
    def state_css(self) -> str:
        return {
            PowerState.RUNNING: "ok",
            PowerState.STOPPED: "off",
            PowerState.SUSPENDED: "warn",
        }.get(self.power_state, "unknown")

    @property
    def ip_display(self) -> str:
        return self.ip_address or "—"

    # -- 路径 --------------------------------------------------------------
    @property
    def dir_path(self) -> str:
        return os.path.dirname(self.vmx_path)

    @property
    def exists(self) -> bool:
        return bool(self.vmx_path) and os.path.isfile(self.vmx_path)

    @property
    def disk_usage_bytes(self) -> int:
        """目录占用，取整方便展示。大目录会慢，只在详情页调用。"""
        total = 0
        try:
            for entry in os.scandir(self.dir_path):
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
        except OSError:
            return 0
        return total

    def mark_state(self, state: str, tools: str = "") -> None:
        self.power_state = state
        if tools:
            self.tools_state = tools
        self.state_checked_at = timezone.now()
        self.save(update_fields=["power_state", "tools_state", "state_checked_at", "updated_at"])


class VmMetric(TimeStampedModel):
    """一台虚拟机的单次采样。

    Workstation 没有宿主侧的性能计数器 API，所以数字来自 **Guest 内采集**
    （``/proc/stat`` + ``free``，见 services/collector.py）——没有 open-vm-tools
    的机器采不到，也没有指标，这是架构上限不是 bug。
    """

    vm = models.ForeignKey(
        VirtualMachine,
        verbose_name="虚拟机",
        on_delete=models.CASCADE,
        related_name="metrics",
    )
    cpu_percent = models.FloatField("CPU %", null=True, blank=True)
    mem_used_mb = models.FloatField("内存占用（MB）", null=True, blank=True)
    mem_percent = models.FloatField("内存 %", null=True, blank=True)

    class Meta:
        verbose_name = "监控采样"
        verbose_name_plural = "监控采样"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["vm", "-created_at"]),
        ]


class Snapshot(TimeStampedModel):
    """快照记录。

    ``vmrun`` 的 ``listSnapshots`` 只给名字，所以这里除了名字之外的信息
    （大小、说明）都是我们自己的账本，同步时会以 vmrun 的结果为准做对账。
    """

    vm = models.ForeignKey(
        VirtualMachine,
        verbose_name="虚拟机",
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    name = models.CharField("快照名称", max_length=128)
    description = models.CharField("描述", max_length=255, blank=True, default="")
    size_bytes = models.BigIntegerField("大小（字节）", default=0)
    parent_name = models.CharField("父快照", max_length=128, blank=True, default="")
    is_current = models.BooleanField("当前快照", default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_snapshots",
    )

    class Meta:
        verbose_name = "快照"
        verbose_name_plural = "快照"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["vm", "name"], name="uniq_snapshot_per_vm"),
        ]

    def __str__(self) -> str:
        return f"{self.vm.name}@{self.name}"
