"""任务执行上下文。

handler 只拿到 ``(job, ctx)`` 两样东西，通过 ``ctx`` 写日志、报进度、检查取消。
这样 handler 不需要知道 worker 怎么调度，也方便单元测试时传一个假 ctx。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.utils import timezone

from vmmanager.models import Job

logger = logging.getLogger("vmmanager.job")


class JobCanceled(RuntimeError):
    """用户请求取消，handler 在检查点抛出它。"""


class JobContext:
    """一次任务执行的可观测通道。"""

    def __init__(self, job: Job, *, worker_id: str = "", heartbeat: threading.Event | None = None):
        self.job = job
        self.worker_id = worker_id
        self._heartbeat = heartbeat
        self._last_cancel_check = 0.0

    # -- 参数 --------------------------------------------------------------
    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload or {}

    def get(self, key: str, default=None):
        return self.payload.get(key, default)

    # -- 输出 --------------------------------------------------------------
    def _emit(self, message: str, level: str) -> None:
        emit = {
            "info": logger.info,
            "ok": logger.info,
            "warn": logger.warning,
            "error": logger.error,
        }.get(level, logger.info)
        emit("[job#%s %s] %s", self.job.pk, self.job.kind, message)

        self.job.append_log(message, level)
        # 写日志意味着任务还活着，顺便刷心跳
        Job.objects.filter(pk=self.job.pk).update(heartbeat_at=timezone.now())

    def info(self, message: str) -> None:
        self._emit(message, "info")

    def ok(self, message: str) -> None:
        self._emit(message, "ok")

    def warn(self, message: str) -> None:
        self._emit(message, "warn")

    def error(self, message: str) -> None:
        self._emit(message, "error")

    def progress(self, percent: int) -> None:
        value = max(0, min(100, int(percent)))
        Job.objects.filter(pk=self.job.pk).update(progress=value, heartbeat_at=timezone.now())
        self.job.progress = value

    # -- 取消 --------------------------------------------------------------
    def check_cancel(self) -> None:
        """在耗时步骤之间调用。节流 2 秒，避免频繁查库。"""
        import time

        now = time.time()
        if now - self._last_cancel_check < 2:
            return
        self._last_cancel_check = now
        requested = Job.objects.filter(pk=self.job.pk).values_list("cancel_requested", flat=True).first()
        if requested:
            raise JobCanceled("Job canceled by user")

    def raise_if_canceled(self) -> None:
        self.check_cancel()


class NullContext:
    """同步执行（测试 / 命令行直接调用）时用的假上下文。"""

    def __init__(self, job: Job | None = None):
        self.job = job
        self.lines: list[str] = []

    @property
    def payload(self) -> dict:
        return self.job.payload if self.job else {}

    def get(self, key, default=None):
        return self.payload.get(key, default)

    def info(self, message: str) -> None:
        self.lines.append(message)
        logger.info(message)

    def ok(self, message: str) -> None:
        self.lines.append(f"✓ {message}")
        logger.info(message)

    def warn(self, message: str) -> None:
        self.lines.append(f"! {message}")
        logger.warning(message)

    def error(self, message: str) -> None:
        self.lines.append(f"✗ {message}")
        logger.error(message)

    def progress(self, percent: int) -> None:
        pass

    def check_cancel(self) -> None:
        pass

    raise_if_canceled = check_cancel
