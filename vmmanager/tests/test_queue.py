"""后台任务队列：去重、抢占、掉线回收。

这几个行为决定了「同一个动作被重复点击」和「worker 崩溃」时系统是否还正确。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from vmmanager.jobs import queue
from vmmanager.models import Job, JobKind, JobStatus, VirtualMachine


@pytest.mark.django_db
def test_enqueue_creates_queued_job():
    job = queue.enqueue(JobKind.SYNC_INVENTORY, label="同步")
    assert job.status == JobStatus.QUEUED
    assert job.progress == 0
    assert queue.pending_count() == 1


@pytest.mark.django_db
def test_dedupe_key_blocks_duplicate_running_job():
    """同一个去重键在任务结束前只能有一个，避免重复点「开机」开两次。"""
    queue.enqueue(JobKind.SYNC_INVENTORY, dedupe_key="sync")
    with pytest.raises(queue.DuplicateJob):
        queue.enqueue(JobKind.SYNC_INVENTORY, dedupe_key="sync")


@pytest.mark.django_db
def test_dedupe_key_released_after_finish():
    first = queue.enqueue(JobKind.SYNC_INVENTORY, dedupe_key="sync")
    first.status = JobStatus.SUCCEEDED
    first.save(update_fields=["status"])

    second = queue.enqueue(JobKind.SYNC_INVENTORY, dedupe_key="sync")
    assert second.pk != first.pk


@pytest.mark.django_db
def test_claim_next_marks_running_with_worker():
    job = queue.enqueue(JobKind.PROBE_IP, label="探测")

    claimed = queue.claim_next("worker-a")

    assert claimed is not None and claimed.pk == job.pk
    claimed.refresh_from_db()
    assert claimed.status == JobStatus.RUNNING
    assert claimed.worker_id == "worker-a"
    assert claimed.started_at is not None
    assert claimed.heartbeat_at is not None


@pytest.mark.django_db
def test_claim_next_returns_none_when_empty():
    assert queue.claim_next("worker-a") is None


@pytest.mark.django_db
def test_claim_next_is_exclusive():
    """两个 worker 同时抢，只能有一个拿到 —— 靠的是原子 UPDATE。"""
    queue.enqueue(JobKind.PROBE_IP)

    first = queue.claim_next("worker-a")
    second = queue.claim_next("worker-b")

    assert first is not None
    assert second is None
    assert Job.objects.filter(status=JobStatus.RUNNING).count() == 1


@pytest.mark.django_db
def test_reclaim_stale_job_from_dead_worker():
    """worker 崩溃后，超过阈值没心跳的任务要能被别的 worker 接手。"""
    job = queue.enqueue(JobKind.PROBE_IP)
    job.status = JobStatus.RUNNING
    job.worker_id = "dead-worker"
    job.started_at = timezone.now() - timedelta(hours=1)
    job.heartbeat_at = timezone.now() - timedelta(hours=1)
    job.save()

    reclaimed = queue.reclaim_stale()

    assert reclaimed == 1
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED
    assert job.worker_id == ""


@pytest.mark.django_db
def test_reclaim_leaves_healthy_job_alone():
    job = queue.enqueue(JobKind.PROBE_IP)
    job.status = JobStatus.RUNNING
    job.worker_id = "alive"
    job.started_at = timezone.now()
    job.heartbeat_at = timezone.now()
    job.save()

    assert queue.reclaim_stale() == 0
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING


@pytest.mark.django_db
def test_request_cancel_sets_flag_for_queued_job():
    job = queue.enqueue(JobKind.PROBE_IP)

    assert queue.request_cancel(job.pk) is True
    job.refresh_from_db()
    assert job.cancel_requested is True


@pytest.mark.django_db
def test_active_job_for_vm_finds_running_job():
    vm = VirtualMachine.objects.create(
        name="vm-q",
        vmx_path=r"D:\VMs\vm-q\vm-q.vmx",
        cpus=1,
        memory_mb=1024,
    )
    assert queue.active_job_for_vm(vm) is None

    job = queue.enqueue(JobKind.POWER_ON, vm=vm)
    assert queue.active_job_for_vm(vm) is not None

    job.status = JobStatus.SUCCEEDED
    job.save(update_fields=["status"])
    assert queue.active_job_for_vm(vm) is None


@pytest.mark.django_db
def test_append_log_accumulates_text():
    """``F() + Value()`` 在 SQLite 上会被当成数字加法，日志曾经因此变成 "0"。"""
    job = queue.enqueue(JobKind.PROBE_IP)
    job.append_log("第一行")
    job.append_log("第二行", level="ok")

    job.refresh_from_db()
    assert "第一行" in job.log
    assert "第二行" in job.log
    assert job.log.count("\n") == 2
    assert job.log.strip() != "0"
