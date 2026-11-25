"""任务重试：原地重置状态、日志保留、权限与入口。

重试与 GitHub Actions 的 Re-run、Airflow 的 Clear task 同一思路——
重置同一个任务行，不创建新任务。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from vmmanager.jobs import queue
from vmmanager.models import Job, JobKind, JobStatus, VirtualMachine

User = get_user_model()


def make_job(**overrides):
    """造一条带完整参数的已失败任务。"""
    vm, _ = VirtualMachine.objects.get_or_create(
        vmx_path=r"D:\VMs\vm-r\vm-r.vmx",
        defaults={"name": "vm-r", "cpus": 1, "memory_mb": 1024},
    )
    defaults = dict(
        kind=JobKind.PROBE_IP,
        status=JobStatus.FAILED,
        payload={"vm_id": vm.pk, "deep": True},
        vm=vm,
        label="vm-r 探测",
        dedupe_key="probe:1",
        error="boom",
        attempts=1,
    )
    defaults.update(overrides)
    job = Job.objects.create(**defaults)
    job.append_log("first attempt failed")
    return job


# ---------------------------------------------------------------------------
# queue.retry_job
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_retry_resets_same_job_row():
    old = make_job()
    old_id = old.pk
    retried = queue.retry_job(old)

    # 同一行，不是新任务
    assert retried.pk == old_id
    retried.refresh_from_db()
    assert retried.status == JobStatus.QUEUED
    assert retried.error == ""
    assert retried.result == {}
    assert retried.progress == 0
    assert retried.started_at is None
    assert retried.finished_at is None
    assert retried.worker_id == ""
    assert retried.heartbeat_at is None
    assert retried.cancel_requested is False
    # attempts 归零，否则 reclaim_stale 会立刻判失败
    assert retried.attempts == 0


@pytest.mark.django_db
def test_retry_keeps_params_and_log_with_separator():
    old = make_job()
    before_log = old.log
    retried = queue.retry_job(old)
    retried.refresh_from_db()

    assert retried.kind == old.kind
    assert retried.payload == old.payload
    assert retried.vm == old.vm
    assert retried.label == old.label
    assert retried.dedupe_key == old.dedupe_key
    # 旧日志保留，且追加了重试分隔线
    assert before_log in retried.log
    assert "第 2 次重试" in retried.log


@pytest.mark.django_db
def test_retry_canceled_job_allowed():
    old = make_job(status=JobStatus.CANCELED)
    queue.retry_job(old)
    old.refresh_from_db()
    assert old.status == JobStatus.QUEUED


@pytest.mark.django_db
def test_retry_rejects_running_and_succeeded():
    running = make_job(status=JobStatus.RUNNING)
    with pytest.raises(queue.NotRetryable):
        queue.retry_job(running)

    ok = make_job(status=JobStatus.SUCCEEDED)
    with pytest.raises(queue.NotRetryable):
        queue.retry_job(ok)


@pytest.mark.django_db
def test_retry_rejects_when_same_dedupe_key_active():
    """同去重键已有另一条未完成任务时不许重试，避免撞唯一约束。"""
    old = make_job()
    queue.enqueue(JobKind.PROBE_IP, dedupe_key="probe:1", label="重复的")
    with pytest.raises(queue.DuplicateJob):
        queue.retry_job(old)


@pytest.mark.django_db
def test_retried_job_can_be_claimed_and_reclaimed():
    """重试后的任务走正常抢占/回收链路，不受旧 attempts 影响。"""
    old = make_job(max_attempts=2)
    queue.retry_job(old)

    claimed = queue.claim_next("worker-a")
    assert claimed is not None and claimed.pk == old.pk

    # 模拟 worker 失联，应被回收重跑而不是直接判失败
    Job.objects.filter(pk=old.pk).update(heartbeat_at=timezone.now() - timedelta(seconds=999))
    assert queue.reclaim_stale() == 1
    old.refresh_from_db()
    assert old.status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# web / api 入口
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_web_retry_resets_and_returns_same_id(client):
    owner = User.objects.create_user(username="jr", password="pw-12345678")
    job = make_job(created_by=owner)
    client.force_login(owner)
    response = client.post(reverse("job_retry", kwargs={"pk": job.pk}))
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"]
    assert payload["job_id"] == job.pk
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED


@pytest.mark.django_db
def test_web_retry_denied_for_other_user(client):
    owner = User.objects.create_user(username="jr2", password="pw-12345678")
    other = User.objects.create_user(username="jr3", password="pw-12345678")
    job = make_job(created_by=owner)
    client.force_login(other)
    response = client.post(reverse("job_retry", kwargs={"pk": job.pk}))
    assert response.status_code == 403


@pytest.mark.django_db
def test_web_retry_staff_can_retry_any(client):
    owner = User.objects.create_user(username="jr4", password="pw-12345678")
    staff = User.objects.create_user(username="jr5", password="pw-12345678", is_staff=True)
    job = make_job(created_by=owner)
    client.force_login(staff)
    response = client.post(reverse("job_retry", kwargs={"pk": job.pk}))
    assert response.status_code == 200


@pytest.mark.django_db
def test_web_retry_running_job_conflict(client):
    user = User.objects.create_user(username="jr6", password="pw-12345678")
    job = make_job(status=JobStatus.RUNNING, created_by=user)
    client.force_login(user)
    response = client.post(reverse("job_retry", kwargs={"pk": job.pk}))
    assert response.status_code == 409


@pytest.mark.django_db
def test_web_retry_requires_login(client):
    job = make_job()
    response = client.post(reverse("job_retry", kwargs={"pk": job.pk}))
    assert response.status_code in (302, 401, 403)


@pytest.mark.django_db
def test_api_retry(client):
    owner = User.objects.create_user(username="jr7", password="pw-12345678")
    job = make_job(created_by=owner)
    client.force_login(owner)
    response = client.post(f"/api/v1/jobs/{job.pk}/retry/")
    assert response.status_code == 200
    assert response.json()["ok"]
