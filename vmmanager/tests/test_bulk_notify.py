"""批量操作与任务终态通知的回归测试。

批量操作的关键约定：逐台独立入队，一台冲突不影响其余；
通知的关键约定：渠道独立、任何失败都不抛出——通知不能影响任务收尾。
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from vmmanager.jobs import worker as worker_mod
from vmmanager.models import Job, JobKind, JobStatus, VirtualMachine
from vmmanager.services import batch, notify

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_notify_channels(settings):
    # settings.VMM 是同一个 dict 对象，pytest-django 的 settings fixture
    # 不会复制它——上一个测试写入的 webhook 会泄漏到下一个测试，必须显式清场。
    for key in ("NOTIFY_WECHAT_WEBHOOK", "NOTIFY_DINGTALK_WEBHOOK", "NOTIFY_EMAIL_TO"):
        settings.VMM[key] = ""
    settings.EMAIL_HOST = ""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def admin(db):
    return get_user_model().objects.create_superuser(
        username="root-user",
        email="",
        password="pw-bulk-admin",
    )


@pytest.fixture
def normal_user(db):
    return get_user_model().objects.create_user(
        username="plain-user",
        password="pw-bulk-user",
    )


@pytest.fixture
def staff_client(admin):
    client = Client()
    client.force_login(admin)
    return client


@pytest.fixture
def user_client(normal_user):
    client = Client()
    client.force_login(normal_user)
    return client


@pytest.fixture
def vms(db, tmp_path):
    """两台真实存在于磁盘上的机器（vmx 文件要存在，路径唯一）。"""
    machines = []
    for name in ("bulk-1", "bulk-2"):
        vmx_file = tmp_path / name / f"{name}.vmx"
        vmx_file.parent.mkdir()
        vmx_file.write_text('displayName = "x"\r\n', encoding="utf-8")
        machines.append(
            VirtualMachine.objects.create(
                name=name,
                vmx_path=str(vmx_file),
                cpus=1,
                memory_mb=512,
            )
        )
    return machines


def _post_json(client, payload):
    return client.post(
        "/vms/bulk/",
        data=json.dumps(payload),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# 批量操作 — 服务层
# ---------------------------------------------------------------------------
def test_bulk_action_enqueues_each_vm(vms, admin):
    result = batch.bulk_action("start", [vm.pk for vm in vms], user=admin)

    assert len(result["jobs"]) == 2
    assert result["errors"] == []
    kinds = list(Job.objects.order_by("id").values_list("kind", flat=True))
    assert kinds == [JobKind.POWER_ON, JobKind.POWER_ON]
    # 每个任务都要关联到自己的机器，页面上才能从任务跳详情
    assert {job.vm_id for job in Job.objects.all()} == {vm.pk for vm in vms}


def test_bulk_action_hard_stop_sets_force(vms, admin):
    result = batch.bulk_action("hard-stop", [vms[0].pk], user=admin)

    assert len(result["jobs"]) == 1
    payload = Job.objects.get().payload
    assert payload["force"] is True


def test_bulk_action_delete_sets_remove_files(vms, admin):
    result = batch.bulk_action("delete", [vms[0].pk], user=admin)

    assert len(result["jobs"]) == 1
    assert Job.objects.get().payload["remove_files"] is True


def test_bulk_action_skips_missing_vm(vms, admin):
    result = batch.bulk_action("start", [vms[0].pk, 99999], user=admin)

    assert len(result["jobs"]) == 1
    assert len(result["errors"]) == 1
    assert "不存在" in result["errors"][0]["message"]


def test_bulk_action_dedupe_conflicts_do_not_block_others(vms, admin):
    """第一台已有未完成的同类任务时，第二台仍应正常入队。"""
    from vmmanager.jobs import queue

    queue.enqueue(
        JobKind.POWER_ON,
        payload={"vm_id": vms[0].pk},
        vm=vms[0],
        dedupe_key=f"{JobKind.POWER_ON}:{vms[0].pk}",
    )

    result = batch.bulk_action("start", [vm.pk for vm in vms], user=admin)

    assert [item["name"] for item in result["jobs"]] == ["bulk-2"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["id"] == vms[0].pk


def test_bulk_action_unknown_action(vms, admin):
    result = batch.bulk_action("explode", [vms[0].pk], user=admin)
    assert result["jobs"] == []
    assert "未知操作" in result["errors"][0]["message"]


# ---------------------------------------------------------------------------
# 批量操作 — 页面层
# ---------------------------------------------------------------------------
def test_bulk_view_enqueues_and_audits(staff_client, vms):
    response = _post_json(staff_client, {"action": "start", "ids": [vm.pk for vm in vms]})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["queued"] == 2
    assert data["failed"] == 0


def test_bulk_view_rejects_delete_for_non_staff(user_client, vms):
    response = _post_json(user_client, {"action": "delete", "ids": [vms[0].pk]})
    assert response.status_code == 403


def test_bulk_view_allows_delete_for_staff(staff_client, vms):
    response = _post_json(staff_client, {"action": "delete", "ids": [vms[0].pk]})
    assert response.status_code == 200
    assert response.json()["queued"] == 1


def test_bulk_view_validates_input(staff_client, vms):
    assert _post_json(staff_client, {"action": "start", "ids": []}).status_code == 400
    assert _post_json(staff_client, {"action": "bogus", "ids": [vms[0].pk]}).status_code == 400
    assert _post_json(staff_client, {"action": "start", "ids": ["abc", "  "]}).status_code == 400


def test_bulk_view_requires_login(vms):
    client = Client()
    assert _post_json(client, {"action": "start", "ids": [vms[0].pk]}).status_code == 302


# ---------------------------------------------------------------------------
# 批量操作 — REST API
# ---------------------------------------------------------------------------
def test_api_bulk(user_client, vms):
    """API 侧走同一服务层，只是认证方式不同。"""
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user=get_user_model().objects.get(username="plain-user"))
    response = api.post(
        "/api/v1/vms/bulk/",
        {"action": "stop", "ids": [vm.pk for vm in vms]},
        format="json",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["queued"] == 2
    kinds = list(Job.objects.order_by("id").values_list("kind", flat=True))
    assert kinds == [JobKind.POWER_OFF, JobKind.POWER_OFF]


def test_api_bulk_rejects_delete_for_non_staff(vms, normal_user):
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user=normal_user)
    response = api.post("/api/v1/vms/bulk/", {"action": "delete", "ids": [vms[0].pk]}, format="json")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 完成通知
# ---------------------------------------------------------------------------
@pytest.fixture
def failed_job(vms):
    """一个已失败的关机任务。"""
    vm = vms[0]
    job = Job.objects.create(
        kind=JobKind.POWER_OFF,
        payload={"vm_id": vm.pk},
        vm=vm,
        label=vm.name,
        status=JobStatus.FAILED,
        error="vmrun exited with code 1: cannot open configuration file",
        finished_at=None,
    )
    return job


def test_notify_wechat(failed_job, settings):
    posts = []
    settings.VMM["NOTIFY_WECHAT_WEBHOOK"] = "https://example.invalid/webhook"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(notify, "_post_json", lambda url, payload: posts.append((url, payload)))
    try:
        sent = notify.notify_job_finished(failed_job)
    finally:
        monkey.undo()

    assert sent == ["wechat"]
    url, payload = posts[0]
    assert url == "https://example.invalid/webhook"
    content = payload["text"]["content"]
    assert "失败" in content
    assert "关机" in content
    assert failed_job.label in content
    # 失败原因的第一行要进正文
    assert "vmrun exited with code 1" in content


def test_notify_dingtalk(failed_job, settings):
    posts = []
    settings.VMM["NOTIFY_DINGTALK_WEBHOOK"] = "https://example.invalid/ding"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(notify, "_post_json", lambda url, payload: posts.append(url))
    try:
        sent = notify.notify_job_finished(failed_job)
    finally:
        monkey.undo()

    assert sent == ["dingtalk"]
    assert posts == ["https://example.invalid/ding"]


def test_notify_email(failed_job, settings):
    calls = []
    settings.VMM["NOTIFY_EMAIL_TO"] = "ops@example.com, boss@example.com"
    settings.EMAIL_HOST = "smtp.example.com"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        notify,
        "send_mail",
        lambda **kwargs: calls.append(kwargs),
    )
    try:
        sent = notify.notify_job_finished(failed_job)
    finally:
        monkey.undo()

    assert sent == ["email"]
    assert calls[0]["recipient_list"] == ["ops@example.com", "boss@example.com"]
    assert "失败" in calls[0]["subject"]


def test_notify_email_skipped_without_smtp(failed_job, settings):
    """没配 EMAIL_HOST 时邮件渠道直接跳过，不该抛异常。"""
    settings.VMM["NOTIFY_EMAIL_TO"] = "ops@example.com"
    settings.EMAIL_HOST = ""
    sent = notify.notify_job_finished(failed_job)
    assert sent == []


def test_notify_no_channels_configured(failed_job, settings):
    settings.VMM["NOTIFY_WECHAT_WEBHOOK"] = ""
    settings.VMM["NOTIFY_DINGTALK_WEBHOOK"] = ""
    settings.VMM["NOTIFY_EMAIL_TO"] = ""
    assert notify.notify_job_finished(failed_job) == []


def test_notify_success_not_sent_by_default(settings):
    """成功默认不打扰——通知太多等于没有通知。"""
    from django.utils import timezone

    job = Job.objects.create(
        kind=JobKind.POWER_ON,
        status=JobStatus.SUCCEEDED,
        finished_at=timezone.now(),
    )
    settings.VMM["NOTIFY_ON_SUCCESS"] = False
    settings.VMM["NOTIFY_WECHAT_WEBHOOK"] = "https://example.invalid/webhook"
    posts = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(notify, "_post_json", lambda url, payload: posts.append(url))
    try:
        assert notify.notify_job_finished(job) == []
    finally:
        monkey.undo()
    assert posts == []


def test_notify_success_sent_when_enabled(settings):
    from django.utils import timezone

    job = Job.objects.create(
        kind=JobKind.POWER_ON,
        status=JobStatus.SUCCEEDED,
        finished_at=timezone.now(),
    )
    settings.VMM["NOTIFY_ON_SUCCESS"] = True
    settings.VMM["NOTIFY_WECHAT_WEBHOOK"] = "https://example.invalid/webhook"
    posts = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(notify, "_post_json", lambda url, payload: posts.append(url))
    try:
        sent = notify.notify_job_finished(job)
    finally:
        monkey.undo()
    assert sent == ["wechat"]


def test_notify_channel_failure_does_not_block_others(failed_job, settings):
    """企微 webhook 挂了，钉钉和邮件仍要发出去。"""
    settings.VMM["NOTIFY_WECHAT_WEBHOOK"] = "https://example.invalid/wechat"
    settings.VMM["NOTIFY_DINGTALK_WEBHOOK"] = "https://example.invalid/ding"
    emails = []

    def fake_post(url, payload):
        if "wechat" in url:
            raise OSError("connection refused")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(notify, "_post_json", fake_post)
    monkey.setattr(notify, "send_mail", lambda **kwargs: emails.append(kwargs))
    try:
        settings.VMM["NOTIFY_EMAIL_TO"] = ""
        sent = notify.notify_job_finished(failed_job)
    finally:
        monkey.undo()

    assert sent == ["dingtalk"]


def test_worker_sends_notification_on_finish(vms, settings, monkeypatch):
    """worker 收尾时必须调用通知，且通知异常不能炸掉 execute。"""
    job = Job.objects.create(
        kind=JobKind.POWER_ON,
        payload={"vm_id": vms[0].pk},
        vm=vms[0],
        label=vms[0].name,
    )

    calls = []
    monkeypatch.setattr(worker_mod, "get_handler", lambda kind: lambda job, ctx: {"ok": True})
    monkeypatch.setattr(notify, "notify_job_finished", lambda j: calls.append(j.pk))

    runner = worker_mod.WorkerRunner(name="w-test")
    runner.execute(job)

    assert calls == [job.pk]
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED
