"""多用户配额：限制每个账号能占用的虚拟机数量与内存总量。

设计取向：

* 只做「创建入口拦截」，不做后台强制回收——超过配额的存量机器不影响使用，
  只是不能创建新的。租户场景里打断别人正在用的机器是灾难；
* staff 用户天然豁免（管理员是操作员不是租户）；
* ``0`` 表示不设上限，没有配额记录的账号也视为不设限——配额是可选的
  治理工具，不该让没配置的人寸步难行。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from .base import TimeStampedModel


class UserQuota(TimeStampedModel):
    """一个账号的资源配额。字段为 0 或缺记录 = 不设限。"""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="账号",
        on_delete=models.CASCADE,
        related_name="quota",
    )
    max_vms = models.PositiveIntegerField("虚拟机数量上限", default=0)
    max_memory_mb = models.PositiveIntegerField("内存总量上限（MB）", default=0)

    class Meta:
        verbose_name = "用户配额"
        verbose_name_plural = "用户配额"

    def __str__(self) -> str:
        return f"{self.user.username}（{self.max_vms} 台 / {self.max_memory_mb} MB）"


class QuotaExceeded(RuntimeError):
    """配额不足。message 已是可直接展示给用户的中文。"""


def usage_for(user) -> dict:
    """一个账号当前的资源占用。"""
    vms = VirtualMachine.objects.filter(owner=user)
    return {
        "vms": vms.count(),
        "memory_mb": sum(v.memory_mb or 0 for v in vms),
    }


def quota_for(user) -> UserQuota | None:
    return getattr(user, "quota", None)


def describe(user) -> str:
    """给人看的一行摘要，如「3 / 5 台 · 8192 / 16384 MB」。"""
    usage = usage_for(user)
    quota = quota_for(user)
    max_vms = quota.max_vms if quota else 0
    max_mem = quota.max_memory_mb if quota else 0
    vms = f"{usage['vms']} / {max_vms} 台" if max_vms else f"{usage['vms']} 台"
    mem = f"{usage['memory_mb']} / {max_mem} MB" if max_mem else f"{usage['memory_mb']} MB"
    return f"{vms} · {mem}"


def check_quota_for_create(user, *, count: int = 1, memory_mb_total: int = 0) -> None:
    """创建前校验。超配额抛 ``QuotaExceeded``（message 可直接展示）。

    ``count`` 是新增台数，``memory_mb_total`` 是新增内存总量（批量时各行
    规格可能不同，由调用方求和传入）。
    """
    if getattr(user, "is_staff", False):
        return
    quota = quota_for(user)
    if quota is None:
        return

    usage = usage_for(user)

    if quota.max_vms and usage["vms"] + count > quota.max_vms:
        raise QuotaExceeded(
            f"已超出虚拟机数量配额：现有 {usage['vms']} 台，本次要建 {count} 台，"
            f"上限 {quota.max_vms} 台。请先删除不再使用的虚拟机，或联系管理员调整配额。"
        )
    if quota.max_memory_mb and usage["memory_mb"] + memory_mb_total > quota.max_memory_mb:
        raise QuotaExceeded(
            f"已超出内存总量配额：现有 {usage['memory_mb']} MB，本次新增 {memory_mb_total} MB，"
            f"上限 {quota.max_memory_mb} MB。请降低规格或联系管理员调整配额。"
        )


from .machine import VirtualMachine  # noqa: E402  底部导入避免循环依赖
