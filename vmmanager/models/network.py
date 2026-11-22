"""IP 租约：防止多台虚拟机拿到同一个地址。"""

from __future__ import annotations

from django.db import models

from .base import TimeStampedModel


class LeaseKind(models.TextChoices):
    STATIC = "static", "静态（nmcli 配置）"
    DHCP_RESERVED = "dhcp_reserved", "DHCP 保留"
    DHCP = "dhcp", "DHCP 动态"


class IpLease(TimeStampedModel):
    """一条 IP 占用记录。

    创建虚拟机前先在这里占位，避免两台机器挑到同一个 IP；虚拟机删除时释放。
    如果集群里有人手工配了 IP，``discover`` 会把冲突标出来。
    """

    ip = models.GenericIPAddressField("IP 地址", unique=True)
    mac = models.CharField("MAC 地址", max_length=32, blank=True, default="")
    vm = models.ForeignKey(
        "VirtualMachine",
        verbose_name="虚拟机",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="leases",
    )
    kind = models.CharField(
        "来源",
        max_length=16,
        choices=LeaseKind.choices,
        default=LeaseKind.STATIC,
    )
    note = models.CharField("备注", max_length=128, blank=True, default="")

    class Meta:
        verbose_name = "IP 租约"
        verbose_name_plural = "IP 租约"
        ordering = ["ip"]
        indexes = [models.Index(fields=["vm"])]

    def __str__(self) -> str:
        return f"{self.ip} → {self.vm or '未分配'}"

    @classmethod
    def reserve(
        cls, ip: str, *, vm=None, mac: str = "", kind: str = LeaseKind.STATIC, note: str = ""
    ) -> IpLease | None:
        """占用一个 IP；已被别人占了就返回 None。"""
        if not ip:
            return None
        lease, created = cls.objects.get_or_create(
            ip=ip,
            defaults={"vm": vm, "mac": mac, "kind": kind, "note": note},
        )
        if not created and lease.vm_id and vm and lease.vm_id != getattr(vm, "pk", None):
            return None
        if not created:
            lease.vm = vm or lease.vm
            lease.mac = mac or lease.mac
            lease.kind = kind
            lease.save(update_fields=["vm", "mac", "kind", "updated_at"])
        return lease

    @classmethod
    def release(cls, ip: str) -> None:
        if ip:
            cls.objects.filter(ip=ip).delete()
