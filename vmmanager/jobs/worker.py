"""worker：消费任务队列的常驻进程。

启动方式：

    python manage.py worker

行为：

* 每次只跑一个任务（vmrun 并发容易互相干扰），跑完立刻取下一个；
* 执行期间由看门狗线程定期刷新心跳，让别的进程知道「我还活着」；
* 任务超过 ``JOB_TIMEOUT`` 时置上取消标记，handler 会在下一个检查点退出；
* 收到 Ctrl+C / SIGTERM 会先把当前任务跑完再退出，不留半截状态。

为什么不用 Celery：这个场景只有一个生产者（Web）和一个消费者（本机 worker），
引入 Redis + Celery 会让「双击就能跑」变成「先装个 Redis」，代价不值。
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from vmmanager.models import Job, JobStatus, Worker
from vmmanager.services import collector, notify

from . import queue
from .base import JobCanceled, JobContext
from .registry import get_handler, load_handlers

logger = logging.getLogger("vmmanager.worker")


class Watchdog(threading.Thread):
    """执行期间维持心跳，并在超时后请求取消。"""

    def __init__(self, job_id: int, worker_name: str, timeout: int):
        super().__init__(daemon=True, name=f"watchdog-{job_id}")
        self.job_id = job_id
        self.worker_name = worker_name
        self.timeout = timeout
        self.stop_event = threading.Event()
        self.live_event = threading.Event()
        self.started = time.time()

    def run(self) -> None:
        interval = max(2, settings.VMM["WORKER_HEARTBEAT"])
        while not self.stop_event.wait(interval):
            try:
                now = timezone.now()
                Job.objects.filter(pk=self.job_id).update(heartbeat_at=now)
                Worker.objects.filter(name=self.worker_name).update(heartbeat_at=now)
                if self.timeout and time.time() - self.started > self.timeout:
                    Job.objects.filter(pk=self.job_id).update(cancel_requested=True)
                    logger.warning("Job #%s exceeded %ss, cancellation requested", self.job_id, self.timeout)
            except Exception:  # 心跳失败不该拖垮任务
                logger.exception("Failed to update heartbeat")

    def stop(self) -> None:
        self.stop_event.set()


class WorkerRunner:
    def __init__(self, *, name: str = "", once: bool = False, poll_interval: float = 2.0):
        load_handlers()
        self.name = name or Worker.new_name()
        self.once = once
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.current_job_id: int | None = None
        self._worker_row: Worker | None = None

    # -- 注册 --------------------------------------------------------------
    def register(self) -> None:
        # 先清掉上次异常退出留下的幽灵记录：worker 名带 pid + 随机后缀，
        # 每次重启都是一条新记录，不清理会越积越多。
        try:
            removed = Worker.purge_stale()
            if removed:
                logger.info("Cleaned up %s stale worker records", removed)
        except Exception:
            logger.exception("Failed to purge stale worker records")

        row, _ = Worker.objects.update_or_create(
            name=self.name,
            defaults={
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "version": "0.2.0",
                "heartbeat_at": timezone.now(),
                "started_at": timezone.now(),
            },
        )
        self._worker_row = row
        logger.info("Worker %s registered (pid=%s)", self.name, os.getpid())

    def unregister(self) -> None:
        """正常退出时删掉自己的记录，让页面立刻反映「没有 worker 在线」。

        被强杀时这里不会执行，那种情况由 ``Worker.purge_stale()`` 兜底。
        """
        Worker.objects.filter(name=self.name).delete()

    # -- 主循环 ------------------------------------------------------------
    def run(self) -> None:
        self.register()
        logger.info("Worker started, poll interval %.1fs", self.poll_interval)
        try:
            while not self.stop_event.is_set():
                close_old_connections()
                queue.reclaim_stale()
                # 定时采集：到点且没在跑就入队一轮（失败只记日志）
                try:
                    collector.maybe_enqueue_metrics()
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to enqueue metrics collection")
                job = queue.claim_next(self.name)
                if job is None:
                    if self.once:
                        logger.info("Queue is empty, exiting (--once mode)")
                        break
                    self.stop_event.wait(self.poll_interval)
                    continue
                self.execute(job)
        finally:
            self.unregister()
            logger.info("Worker stopped")

    def request_stop(self, *_args) -> None:
        if self.current_job_id:
            logger.warning("Exit signal received, waiting for job #%s to finish...", self.current_job_id)
        else:
            logger.info("Exit signal received")
        self.stop_event.set()

    # -- 执行 --------------------------------------------------------------
    def execute(self, job: Job) -> None:
        self.current_job_id = job.pk
        Worker.objects.filter(name=self.name).update(current_job=job)
        handler = get_handler(job.kind)

        timeout = int(settings.VMM["JOB_TIMEOUT"] or 0)
        watchdog = Watchdog(job.pk, self.name, timeout)
        watchdog.start()
        ctx = JobContext(job, worker_id=self.name)

        started = time.time()
        try:
            if handler is None:
                raise RuntimeError(f"No handler registered for job kind {job.kind}")

            result = handler(job, ctx) or {}
            duration = int(time.time() - started)
            Job.objects.filter(pk=job.pk).update(
                status=JobStatus.SUCCEEDED,
                finished_at=timezone.now(),
                progress=100,
                result=result,
                error="",
                dedupe_key="",  # 释放去重键，允许下次重新入队
                heartbeat_at=timezone.now(),
            )
            logger.info("Job #%s completed (%ss)", job.pk, duration)

        except JobCanceled as exc:
            Job.objects.filter(pk=job.pk).update(
                status=JobStatus.CANCELED,
                finished_at=timezone.now(),
                error=str(exc),
                dedupe_key="",
            )
            job.append_log("Job canceled")
            logger.info("Job #%s canceled", job.pk)

        except Exception as exc:  # noqa: BLE001 - 任务失败必须落库，不能吞掉
            message = str(exc) or exc.__class__.__name__
            Job.objects.filter(pk=job.pk).update(
                status=JobStatus.FAILED,
                finished_at=timezone.now(),
                error=message[:2000],
                dedupe_key="",
                heartbeat_at=timezone.now(),
            )
            job.append_log(message, "error")
            logger.error("Job #%s failed: %s\n%s", job.pk, message, traceback.format_exc())

        finally:
            watchdog.stop()
            self.current_job_id = None
            Worker.objects.filter(name=self.name).update(current_job=None, heartbeat_at=timezone.now())
            self._notify(job)
            close_old_connections()

    @staticmethod
    def _notify(job: Job) -> None:
        """终态通知。失败只记日志——通知永远不该把任务收尾搞砸。"""
        try:
            job.refresh_from_db(fields=["status", "error", "finished_at"])
            notify.notify_job_finished(job)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send notification for job #%s", job.pk)


def run_forever(*, name: str = "", once: bool = False) -> None:
    """供 manage.py 与独立入口调用。"""
    runner = WorkerRunner(name=name, once=once)

    def _handler(signum, _frame):
        runner.request_stop()
        # 第二次 Ctrl+C 强制退出
        if runner.stop_event.is_set() and runner.current_job_id:
            signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)

    runner.run()


def main() -> None:  # pragma: no cover - 命令行入口
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")
    import django

    django.setup()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
