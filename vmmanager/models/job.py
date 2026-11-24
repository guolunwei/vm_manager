"""持久化任务队列。

设计要点（这也是本项目与「随便开个线程」最大的区别）：

* 任务落库，进程重启不会丢；worker 重启后能接手排队任务。
* ``status=queued`` 的行本身就是队列，抢任务用一条带 ``WHERE status='queued'``
  的原子 UPDATE，SQLite 下也安全。
* 执行中的任务靠 ``heartbeat_at`` 证明存活；worker 崩溃后由别的 worker
  按 ``WORKER_STALE_AFTER`` 回收，避免任务永远卡在 running。
* 日志追加写 ``log`` 字段，前端按 offset 拉增量，避免每次轮询传全量。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Value
from django.db.models.functions import Coalesce, Concat
from django.utils import timezone

from .base import TimeStampedModel


class JobKind(models.TextChoices):
    PROVISION = "provision", "创建虚拟机"
    POWER_ON = "power_on", "开机"
    POWER_OFF = "power_off", "关机"
    POWER_RESET = "power_reset", "重启"
    SUSPEND = "suspend", "挂起"
    DELETE = "delete", "删除"
    SET_IP = "set_ip", "配置 IP"
    PROBE_IP = "probe_ip", "探测 IP"
    SYNC_INVENTORY = "sync_inventory", "同步磁盘"
    SNAPSHOT_CREATE = "snapshot_create", "创建快照"
    SNAPSHOT_REVERT = "snapshot_revert", "回滚快照"
    SNAPSHOT_DELETE = "snapshot_delete", "删除快照"
    RECONFIGURE = "reconfigure", "重新配置"
    RESIZE = "resize", "调整规格"
    GROW_DISK = "grow_disk", "扩容磁盘"
    CLONE_VM = "clone_vm", "克隆虚拟机"
    METRICS = "metrics", "指标采集"
    EXPORT_OVF = "export_ovf", "导出 OVA"


class JobStatus(models.TextChoices):
    QUEUED = "queued", "排队中"
    RUNNING = "running", "运行中"
    SUCCEEDED = "succeeded", "成功"
    FAILED = "failed", "失败"
    CANCELED = "canceled", "已取消"

    @classmethod
    def finished(cls) -> tuple[str, ...]:
        return (cls.SUCCEEDED, cls.FAILED, cls.CANCELED)


class Job(TimeStampedModel):
    """一次后台作业。既是任务定义，也是执行日志载体。"""

    kind = models.CharField("类型", max_length=32, choices=JobKind.choices)
    status = models.CharField(
        "状态",
        max_length=16,
        choices=JobStatus.choices,
        default=JobStatus.QUEUED,
        db_index=True,
    )
    payload = models.JSONField("参数", default=dict, blank=True)

    vm = models.ForeignKey(
        "VirtualMachine",
        verbose_name="虚拟机",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )
    template = models.ForeignKey(
        "VmTemplate",
        verbose_name="模板",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )
    # 虚拟机可能被删掉，展示用的名字要冗余一份
    label = models.CharField("目标标签", max_length=128, blank=True, default="")

    progress = models.PositiveSmallIntegerField("进度", default=0)
    log = models.TextField("日志", blank=True, default="")
    error = models.TextField("错误", blank=True, default="")
    result = models.JSONField("结果", default=dict, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="发起人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )

    queued_at = models.DateTimeField("入队时间", default=timezone.now, db_index=True)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)

    worker_id = models.CharField("worker", max_length=64, blank=True, default="")
    heartbeat_at = models.DateTimeField("心跳", null=True, blank=True)
    attempts = models.PositiveSmallIntegerField("尝试次数", default=0)
    max_attempts = models.PositiveSmallIntegerField("最大尝试次数", default=1)
    cancel_requested = models.BooleanField("已请求取消", default=False)
    dedupe_key = models.CharField(
        "去重键",
        max_length=128,
        blank=True,
        default="",
        help_text="同一去重键同时只能有一个未完成任务",
    )

    class Meta:
        verbose_name = "任务"
        verbose_name_plural = "任务"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "queued_at"]),
            models.Index(fields=["status", "heartbeat_at"]),
            models.Index(fields=["kind", "-created_at"]),
        ]
        constraints = [
            # 去重键只在「任务还没结束」时唯一。之前写成只排除空串，等于全局唯一，
            # 结果是同一个动作（比如「同步磁盘」）成功执行过一次之后，数据库层面
            # 就再也插不进同 key 的任务，功能被永久锁死。
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=(~models.Q(dedupe_key="") & ~models.Q(status__in=JobStatus.finished())),
                name="uniq_active_dedupe_key",
            ),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.get_kind_display()} {self.label}（{self.get_status_display()}）"

    # -- 状态判断 ----------------------------------------------------------
    @property
    def is_finished(self) -> bool:
        return self.status in JobStatus.finished()

    @property
    def duration(self) -> int:
        end = self.finished_at or timezone.now()
        start = self.started_at or self.queued_at
        return max(0, int((end - start).total_seconds()))

    @property
    def log_lines(self) -> list[str]:
        return [line for line in self.log.splitlines() if line.strip()]

    @property
    def status_css(self) -> str:
        return {
            JobStatus.QUEUED: "queued",
            JobStatus.RUNNING: "running",
            JobStatus.SUCCEEDED: "ok",
            JobStatus.FAILED: "failed",
            JobStatus.CANCELED: "off",
        }.get(self.status, "unknown")

    @property
    def is_stale(self) -> bool:
        """执行中但心跳太久没更新 —— 说明 worker 挂了。"""
        if self.status != JobStatus.RUNNING or not self.heartbeat_at:
            return False
        threshold = settings.VMM["WORKER_STALE_AFTER"]
        return (timezone.now() - self.heartbeat_at).total_seconds() > threshold

    # -- 日志 --------------------------------------------------------------
    def append_log(self, message: str, level: str = "info") -> None:
        """追加一行日志。

        用 ``Concat`` 而不是 ``F("log") + chunk``：后者在 SQLite 上会被当成
        数值加法（``'abc' + 'def'`` = 0），日志会全部变成 "0"。
        """
        stamp = timezone.localtime().strftime("%H:%M:%S")
        prefix = {"info": "", "ok": "✓ ", "warn": "! ", "error": "✗ "}.get(level, "")
        chunk = f"[{stamp}] {prefix}{message}\n"
        type(self).objects.filter(pk=self.pk).update(
            log=Concat(Coalesce("log", Value("")), Value(chunk)),
            updated_at=timezone.now(),
        )
        self.log = (self.log or "") + chunk


class Worker(TimeStampedModel):
    """worker 进程注册表，用于在页面上展示「谁在跑」。"""

    name = models.CharField("名称", max_length=64, unique=True)
    hostname = models.CharField("主机名", max_length=128, blank=True, default="")
    pid = models.PositiveIntegerField("PID", null=True, blank=True)
    version = models.CharField("版本", max_length=32, blank=True, default="")
    current_job = models.ForeignKey(
        Job,
        verbose_name="当前任务",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    heartbeat_at = models.DateTimeField("心跳", default=timezone.now, db_index=True)
    started_at = models.DateTimeField("启动时间", default=timezone.now)

    class Meta:
        verbose_name = "worker"
        verbose_name_plural = "worker"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_online(self) -> bool:
        """心跳还在新鲜期内即视为在线。

        进程被强杀时 ``unregister`` 不会执行，只能靠心跳过期来判定死亡，
        所以「在线」必须由时间推出来，不能只看记录是否存在。
        """
        stale_after = settings.VMM.get("WORKER_STALE_AFTER", 120)
        return (timezone.now() - self.heartbeat_at).total_seconds() < stale_after

    @classmethod
    def online(cls):
        """心跳新鲜的 worker 集合。页面判断「队列有没有人在消费」用它。"""
        stale_after = settings.VMM.get("WORKER_STALE_AFTER", 120)
        return cls.objects.filter(heartbeat_at__gte=timezone.now() - timedelta(seconds=stale_after))

    @classmethod
    def purge_stale(cls) -> int:
        """删掉心跳过期的记录，返回删除条数。

        每次启动都会生成新的 worker 名（含 pid 与随机后缀），如果不清理，
        注册表会无限增长，页面上全是早已退出的幽灵 worker。
        """
        stale_after = settings.VMM.get("WORKER_STALE_AFTER", 120)
        deleted, _ = cls.objects.filter(
            heartbeat_at__lt=timezone.now() - timedelta(seconds=stale_after * 2)
        ).delete()
        return deleted

    @classmethod
    def new_name(cls) -> str:
        import os
        import socket

        return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
