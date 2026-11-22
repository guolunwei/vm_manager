"""队列操作：入队、抢任务、超时回收、取消。

SQLite 没有 ``SELECT ... FOR UPDATE``，所以抢任务用「带条件的原子 UPDATE」：
先查到候选 id，再 ``UPDATE ... WHERE id=? AND status='queued'``，
只有影响行数为 1 的那个 worker 才真正拿到任务。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from vmmanager.models import Job, JobKind, JobStatus, VirtualMachine, VmTemplate

logger = logging.getLogger("vmmanager.queue")


class QueueFull(RuntimeError):
    pass


class DuplicateJob(RuntimeError):
    """同一去重键已有未完成任务。"""


def enqueue(
    kind: str,
    *,
    payload: dict | None = None,
    vm: VirtualMachine | None = None,
    template: VmTemplate | None = None,
    label: str = "",
    user=None,
    dedupe_key: str = "",
    max_attempts: int = 1,
    priority_note: str = "",
) -> Job:
    """把任务放进队列。"""
    if dedupe_key:
        existing = (
            Job.objects.filter(
                dedupe_key=dedupe_key,
            )
            .exclude(status__in=JobStatus.finished())
            .first()
        )
        if existing:
            phase = "正在运行" if existing.status == JobStatus.RUNNING else "已在排队中"
            raise DuplicateJob(f"该操作{phase}（#{existing.pk} {existing.get_kind_display()}），请等待其完成")

    job = Job.objects.create(
        kind=kind,
        payload=payload or {},
        vm=vm,
        template=template,
        label=(label or (vm.name if vm else "") or (template.name if template else ""))[:128],
        created_by=user if getattr(user, "pk", None) else None,
        dedupe_key=dedupe_key,
        max_attempts=max(1, max_attempts),
        queued_at=timezone.now(),
    )
    if priority_note:
        job.append_log(priority_note)
    logger.info("Enqueued #%s %s %s", job.pk, kind, job.label)
    return job


def claim_next(worker_id: str) -> Job | None:
    """原子地抢一个排队任务。

    只抢 ``queued_at`` 已到期（<= now）的任务——定时任务（如指标采集）会把
    下一轮提前入队并写未来的时间戳，没有这个过滤会被立刻抢出来空转。
    """
    now = timezone.now()
    candidate = (
        Job.objects.filter(status=JobStatus.QUEUED, queued_at__lte=now)
        .order_by("queued_at", "id")
        .values_list("pk", flat=True)
        .first()
    )
    if candidate is None:
        return None

    with transaction.atomic():
        updated = Job.objects.filter(pk=candidate, status=JobStatus.QUEUED).update(
            status=JobStatus.RUNNING,
            worker_id=worker_id,
            started_at=now,
            heartbeat_at=now,
            attempts=F("attempts") + 1,
        )
    if not updated:
        # 被别的 worker 抢先了
        return None

    job = Job.objects.select_related("vm", "template").get(pk=candidate)
    job.append_log(f"Claimed by worker {worker_id}")
    return job


def reclaim_stale() -> int:
    """回收心跳超时的 running 任务。

    还有重试机会的放回队列，否则判失败 —— 否则任务会永远卡在「执行中」。
    """
    threshold = timezone.now() - timedelta(seconds=settings.VMM["WORKER_STALE_AFTER"])
    stale = Job.objects.filter(status=JobStatus.RUNNING, heartbeat_at__lt=threshold)

    reclaimed = 0
    for job in stale:
        if job.attempts < job.max_attempts:
            Job.objects.filter(pk=job.pk).update(
                status=JobStatus.QUEUED,
                worker_id="",
                heartbeat_at=None,
                progress=0,
                queued_at=timezone.now(),
            )
            job.append_log(f"Worker heartbeat timed out (>{settings.VMM['WORKER_STALE_AFTER']}s), re-queued")
            logger.warning("Reclaimed job #%s, re-queued", job.pk)
        else:
            Job.objects.filter(pk=job.pk).update(
                status=JobStatus.FAILED,
                finished_at=timezone.now(),
                error="The worker running this job was lost (crashed or killed), job did not finish",
            )
            job.append_log("✗ Worker lost, job marked as failed")
            logger.error("Job #%s marked failed: worker lost heartbeat", job.pk)
        reclaimed += 1
    return reclaimed


def request_cancel(job_id: int, *, user=None) -> bool:
    """请求取消。排队中的直接取消；执行中的打标记，由 handler 在检查点退出。"""
    now = timezone.now()
    job = Job.objects.filter(pk=job_id).first()
    if job is None or job.is_finished:
        return False

    if job.status == JobStatus.QUEUED:
        Job.objects.filter(pk=job_id).update(
            status=JobStatus.CANCELED,
            finished_at=now,
            cancel_requested=True,
        )
        job.append_log("Canceled while queued")
        return True

    Job.objects.filter(pk=job_id).update(cancel_requested=True)
    job.append_log("Cancellation requested, will exit at the next checkpoint")
    return True


def pending_count() -> int:
    return Job.objects.filter(status__in=[JobStatus.QUEUED, JobStatus.RUNNING]).count()


def active_job_for_vm(vm: VirtualMachine) -> Job | None:
    """某台机器当前是否有未完成任务（用于页面上展示「任务中」）。"""
    return Job.objects.filter(vm=vm).exclude(status__in=JobStatus.finished()).order_by("id").first()


def pending_count_for_vm(vm: VirtualMachine) -> int:
    """该机器前面还排着几个未完成任务。

    刻意不在入队前用「有未完成任务」硬拦：一台机器上点完「开机」再点「关机」，
    按点击顺序执行本来就是用户的意图。硬拦的结果是所有按钮一起失效，
    用户只会以为界面坏了。真正需要防的是「同类动作重复提交」，
    那由 ``dedupe_key`` 负责。
    """
    return Job.objects.filter(vm=vm).exclude(status__in=JobStatus.finished()).count()


def purge_old_jobs(days: int = 30) -> int:
    """清理历史任务，保留最近 N 天。"""
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Job.objects.filter(
        status__in=JobStatus.finished(),
        finished_at__lt=cutoff,
    ).delete()
    return deleted


KINDS = JobKind
