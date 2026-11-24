"""任务重试：状态校验、参数继承、权限与入口。"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from vmmanager.jobs import queue
from vmmanager.models import Job, JobKind, JobStatus, VirtualMachine

User = get_user_model()


def make_job(**overrides):
    """造一条带完整参数的任务，方便验证重试时逐字段继承。"""
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
    )
    defaults.update(overrides)
    job = Job.objects.create(**defaults)
    job.append_log("first attempt failed")
    return job


# ---------------------------------------------------------------------------
# queue.retry_job
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_retry_failed_job_creates_new_job_with_same_params():
    old = make_job()
    new = queue.retry_job(old)

    assert new.pk != old.pk
    assert new.kind == old.kind
    assert new.payload == old.payload
    assert new.vm == old.vm
    assert new.label == old.label
    assert new.dedupe_key == old.dedupe_key
    assert new.status == JobStatus.QUEUED
    assert f"Retry of job #{old.pk}" in new.log


@pytest.mark.django_db
def test_retry_canceled_job_allowed():
    old = make_job(status=JobStatus.CANCELED)
    new = queue.retry_job(old)
    assert new.status == JobStatus.QUEUED


@pytest.mark.django_db
def test_retry_rejects_running_and_succeeded():
    running = make_job(status=JobStatus.RUNNING)
    with pytest.raises(queue.NotRetryable):
        queue.retry_job(running)

    ok = make_job(status=JobStatus.SUCCEEDED)
    with pytest.raises(queue.NotRetryable):
        queue.retry_job(ok)


@pytest.mark.django_db
def test_retry_inherits_dedupe_blocking():
    """原任务重试后再次失败，用同一去重键时若已有未完成任务要被拦。"""
    old = make_job()
    new = queue.retry_job(old)
    with pytest.raises(queue.DuplicateJob):
        queue.retry_job(old)  # 新任务还在排队，同键再排会被去重拦下
    assert new.pk


@pytest.mark.django_db
def test_retry_keeps_original_log_intact():
    """重试不改写原任务——日志是排查证据。"""
    old = make_job()
    before = old.log
    queue.retry_job(old)
    old.refresh_from_db()
    assert old.log == before
    assert old.status == JobStatus.FAILED


# ---------------------------------------------------------------------------
# web / api 入口
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_web_retry_enqueue(client):
    owner = User.objects.create_user(username="jr", password="pw-12345678")
    job = make_job(created_by=owner)
    client.force_login(owner)
    response = client.post(reverse("job_retry", kwargs={"pk": job.pk}))
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"]
    assert payload["retried_from"] == job.pk
    assert payload["job_id"] != job.pk


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
