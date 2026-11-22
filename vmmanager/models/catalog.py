"""模板与规格：创建虚拟机的「模具」。"""

from __future__ import annotations

import os

from django.core.exceptions import ValidationError
from django.db import models

from .base import TimeStampedModel, check_constraint


class OsFamily(models.TextChoices):
    LINUX = "linux", "Linux"
    WINDOWS = "windows", "Windows"
    OTHER = "other", "其他"


class NetworkMode(models.TextChoices):
    HOSTONLY = "hostonly", "仅主机"
    NAT = "nat", "NAT"
    BRIDGED = "bridged", "桥接"


class VmTemplate(TimeStampedModel):
    """克隆源：一台装好系统并打好快照的「母机」。

    模板只存元信息，真正的克隆动作在 :mod:`vmmanager.services.provision`。
    """

    name = models.CharField("模板名称", max_length=64, unique=True)
    vmx_path = models.CharField("vmx 路径", max_length=512)
    snapshot = models.CharField(
        "快照名称",
        max_length=64,
        blank=True,
        default="",
        help_text="留空则从当前状态克隆而非快照",
    )

    os_family = models.CharField(
        "客户机系统",
        max_length=16,
        choices=OsFamily.choices,
        default=OsFamily.LINUX,
    )
    network_mode = models.CharField(
        "网络模式",
        max_length=16,
        choices=NetworkMode.choices,
        default=NetworkMode.HOSTONLY,
    )
    guest_user = models.CharField("客户机用户名", max_length=64, default="root")
    guest_password = models.CharField(
        "客户机密码",
        max_length=128,
        blank=True,
        default="",
        help_text="仅在创建时通过 vmrun 在客户机内配置主机名/IP 使用。"
        "明文存储在本地数据库中，请限制 data/ 目录的权限",
    )
    guest_connection = models.CharField(
        "连接名",
        max_length=64,
        default="eth0",
        help_text="nmcli 连接名。CentOS 7 配置静态 IPv4 时通常与网卡名一致，如 eth0 / ens33",
    )

    prefix_len = models.PositiveSmallIntegerField("子网前缀", default=24)
    gateway = models.CharField("网关", max_length=64, blank=True, default="")
    dns = models.CharField("DNS", max_length=64, blank=True, default="")

    default_cpus = models.PositiveSmallIntegerField("默认 vCPU", default=2)
    default_memory_mb = models.PositiveIntegerField("默认内存（MB）", default=4096)
    default_flavor = models.ForeignKey(
        "Flavor",
        verbose_name="默认规格",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for_templates",
    )

    deploy_ssh_key = models.BooleanField("下发本机 SSH 公钥", default=True)
    is_active = models.BooleanField("启用", default=True, db_index=True)
    notes = models.TextField("备注", blank=True, default="")

    class Meta:
        verbose_name = "模板"
        verbose_name_plural = "模板"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if self.vmx_path and not self.vmx_path.lower().endswith(".vmx"):
            raise ValidationError({"vmx_path": "必须是 .vmx 文件路径"})
        if self.default_cpus < 1:
            raise ValidationError({"default_cpus": "至少 1 个 vCPU"})
        if self.default_memory_mb < 256:
            raise ValidationError({"default_memory_mb": "内存至少 256 MB"})

    @property
    def exists(self) -> bool:
        return bool(self.vmx_path) and os.path.isfile(self.vmx_path)

    @property
    def dir_path(self) -> str:
        return os.path.dirname(self.vmx_path)

    @property
    def snapshot_label(self) -> str:
        return self.snapshot or "当前状态"

    @property
    def is_running(self) -> bool:
        """模板此刻是否在宿主上运行。

        模板正常应保持关机：链接克隆依赖的快照是只读的，但运行中的模板一旦被
        写入（例如临时进去打补丁），基础磁盘就偏离快照，已有克隆的一致性会受影响，
        而且会白占内存。界面上必须能看见这个状态。

        注意：列表页应改用 ``vmrun.list_running_set()`` 一次取全集再比对，
        本属性每次都会拉起一个 vmrun 子进程，只适合单条查询。
        """
        if not self.exists:
            return False

        from ..services import vmrun

        try:
            return vmrun.is_running(self.vmx_path)
        except Exception:
            return False

    @property
    def memory_display(self) -> str:
        return f"{self.default_memory_mb / 1024:g} GB"

    @property
    def default_spec_display(self) -> str:
        return f"{self.default_cpus} vCPU · {self.memory_display}"


class Flavor(TimeStampedModel):
    """规格：vCPU + 内存的常用组合，创建时一键选。"""

    name = models.CharField("规格名称", max_length=32, unique=True)
    cpus = models.PositiveSmallIntegerField("vCPU", default=2)
    memory_mb = models.PositiveIntegerField("内存（MB）", default=4096)
    description = models.CharField("描述", max_length=128, blank=True, default="")
    sort_order = models.PositiveSmallIntegerField("排序", default=100, db_index=True)
    is_active = models.BooleanField("启用", default=True, db_index=True)

    class Meta:
        verbose_name = "规格"
        verbose_name_plural = "规格"
        ordering = ["sort_order", "memory_mb", "cpus"]
        constraints = [
            check_constraint(condition=models.Q(cpus__gte=1), name="flavor_cpus_gte_1"),
            check_constraint(condition=models.Q(memory_mb__gte=256), name="flavor_mem_gte_256"),
        ]

    def __str__(self) -> str:
        return f"{self.name}（{self.cpus}C/{self.memory_display}）"

    @property
    def memory_display(self) -> str:
        return f"{self.memory_mb / 1024:g} GB"

    @property
    def spec_display(self) -> str:
        return f"{self.cpus} vCPU · {self.memory_display}"
