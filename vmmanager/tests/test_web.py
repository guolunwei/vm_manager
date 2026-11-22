"""页面层的回归测试。

这里覆盖一类容易漏掉的问题：**模板也是虚拟机，但它不在一览表里**。
宿主上明明有机器在跑，界面却显示「运行中 0」，会直接误导管理员。
测试一律不真正启动虚拟机——``vmrun`` 被 monkeypatch 掉。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from vmmanager.models import VmTemplate
from vmmanager.services import vmrun

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_superuser(
        username="root-user",
        email="",
        password="pw-admin-9x2",
    )


@pytest.fixture
def staff_client(admin):
    client = Client()
    client.force_login(admin)
    return client


@pytest.fixture
def template(db, tmp_path):
    """一个 vmx 文件真实存在的模板，避免 exists 判成 False。"""
    vmx_file = tmp_path / "base.vmx"
    vmx_file.write_text('displayName = "base"\r\n', encoding="utf-8")
    return VmTemplate.objects.create(
        name="base-template",
        vmx_path=str(vmx_file),
        guest_user="root",
        default_cpus=2,
        default_memory_mb=2048,
    )


def test_template_is_running_property(template, monkeypatch):
    """属性要把 vmrun 的答案如实转成布尔，不能永远 False。"""
    monkeypatch.setattr(vmrun, "is_running", lambda path: True)
    assert template.is_running is True

    monkeypatch.setattr(vmrun, "is_running", lambda path: False)
    assert template.is_running is False


def test_template_is_running_is_false_when_vmrun_explodes(template, monkeypatch):
    """vmrun 挂掉时页面不能 500，只能当作「不知道 = 没在跑」。"""

    def boom(path):
        raise RuntimeError("vmrun unavailable")

    monkeypatch.setattr(vmrun, "is_running", boom)
    assert template.is_running is False


def test_dashboard_reports_running_template(staff_client, template, monkeypatch):
    """运行中的模板必须在仪表盘上露出，否则统计会骗人。"""
    monkeypatch.setattr(vmrun, "list_running_set", lambda: {vmrun.normpath(template.vmx_path)})

    body = staff_client.get("/").content.decode()
    assert "个模板正在运行" in body
    assert template.name in body


def test_dashboard_silent_when_all_templates_stopped(staff_client, template, monkeypatch):
    monkeypatch.setattr(vmrun, "list_running_set", lambda: set())

    body = staff_client.get("/").content.decode()
    assert "个模板正在运行" not in body


def test_dashboard_survives_vmrun_failure(staff_client, template, monkeypatch):
    """宿主上没有 vmrun（或它报错）时，仪表盘仍要能打开。"""

    def boom():
        raise RuntimeError("vmrun missing")

    monkeypatch.setattr(vmrun, "list_running_set", boom)
    assert staff_client.get("/").status_code == 200


def test_settings_marks_running_template(staff_client, template, monkeypatch):
    monkeypatch.setattr(vmrun, "list_running_set", lambda: {vmrun.normpath(template.vmx_path)})

    body = staff_client.get("/settings/").content.decode()
    assert "pill-warn" in body
    assert "运行中" in body
    # 运行中应给「关机」，而不是「开机」
    assert 'value="stop"' in body
    assert 'value="start"' not in body


def test_settings_offers_start_when_stopped(staff_client, template, monkeypatch):
    monkeypatch.setattr(vmrun, "list_running_set", lambda: set())

    body = staff_client.get("/settings/").content.decode()
    assert 'value="start"' in body
    assert 'value="stop"' not in body


def test_template_power_start_calls_vmrun(staff_client, template, monkeypatch):
    calls = []
    monkeypatch.setattr(vmrun, "start", lambda path, **kw: calls.append(("start", path)))

    response = staff_client.post(f"/settings/templates/{template.pk}/power/", {"action": "start"})

    assert response.status_code == 302
    assert calls == [("start", template.vmx_path)]


def test_template_power_stop_defaults_to_soft(staff_client, template, monkeypatch):
    calls = []
    monkeypatch.setattr(vmrun, "stop", lambda path, **kw: calls.append(kw.get("hard")))

    staff_client.post(f"/settings/templates/{template.pk}/power/", {"action": "stop"})

    assert calls == [None]  # 没传 hard，走软关机（内部失败会自动退硬关）


def test_template_power_rejects_non_staff(template, monkeypatch):
    """普通用户不能开关模板电源。"""
    user = get_user_model().objects.create_user(username="plain", password="pw-plain")
    client = Client()
    client.force_login(user)
    called = []
    monkeypatch.setattr(vmrun, "start", lambda path, **kw: called.append(path))

    response = client.post(f"/settings/templates/{template.pk}/power/", {"action": "start"})

    assert response.status_code == 302
    assert called == []


def test_template_power_reports_vmrun_error_without_500(staff_client, template, monkeypatch):
    def boom(path, **kw):
        raise vmrun.VmrunError("vmrun 出错")

    monkeypatch.setattr(vmrun, "stop", boom)

    response = staff_client.post(
        f"/settings/templates/{template.pk}/power/",
        {"action": "stop"},
        follow=True,
    )

    assert response.status_code == 200
    assert "操作失败" in response.content.decode()


def test_template_power_rejects_unknown_action(staff_client, template, monkeypatch):
    monkeypatch.setattr(vmrun, "start", lambda path, **kw: None)
    monkeypatch.setattr(vmrun, "stop", lambda path, **kw: None)

    response = staff_client.post(
        f"/settings/templates/{template.pk}/power/",
        {"action": "explode"},
        follow=True,
    )

    assert response.status_code == 200
    assert "未知的电源操作" in response.content.decode()


# ---------------------------------------------------------------------------
# Worker 在线状态：任务「永远排队中」的可解释性
# ---------------------------------------------------------------------------
def test_worker_online_is_decided_by_heartbeat(db):
    """进程被强杀时来不及注销，只能靠心跳判断死没死。"""
    from datetime import timedelta

    from django.utils import timezone

    from vmmanager.models import Worker

    fresh = Worker.objects.create(name="w-fresh", heartbeat_at=timezone.now())
    Worker.objects.create(
        name="w-dead",
        heartbeat_at=timezone.now() - timedelta(seconds=999),
    )

    assert fresh.is_online is True
    assert Worker.online().count() == 1
    assert Worker.online().first().name == "w-fresh"


def test_purge_stale_removes_old_workers_only(db):
    from datetime import timedelta

    from django.utils import timezone

    from vmmanager.models import Worker

    Worker.objects.create(name="w-live", heartbeat_at=timezone.now())
    Worker.objects.create(
        name="w-ghost",
        heartbeat_at=timezone.now() - timedelta(hours=5),
    )

    assert Worker.purge_stale() >= 1
    assert list(Worker.objects.values_list("name", flat=True)) == ["w-live"]


def test_dashboard_warns_when_jobs_pending_without_worker(staff_client, db):
    """有排队任务却没有在线 worker —— 必须直说是 worker 没启动。"""
    from vmmanager.jobs import queue
    from vmmanager.models import JobKind

    queue.enqueue(JobKind.PROBE_IP, label="探测")

    body = staff_client.get("/").content.decode()
    assert "没有 worker 在消费" in body


def test_dashboard_quiet_when_worker_is_online(staff_client, db):
    from django.utils import timezone

    from vmmanager.jobs import queue
    from vmmanager.models import JobKind, Worker

    Worker.objects.create(name="w-on", heartbeat_at=timezone.now())
    queue.enqueue(JobKind.PROBE_IP, label="探测")

    body = staff_client.get("/").content.decode()
    assert "没有 worker 在消费" not in body


# ---------------------------------------------------------------------------
# 动作入队：一个排队中的任务不能让整台机器的按钮失效
# ---------------------------------------------------------------------------
@pytest.fixture
def vm(db, template):
    from vmmanager.models import VirtualMachine

    return VirtualMachine.objects.create(
        name="vm-alpha",
        hostname="vm-alpha",
        vmx_path=str(template.vmx_path).replace("base.vmx", "vm-alpha.vmx"),
        template=template,
        cpus=2,
        memory_mb=2048,
        power_state="stopped",
    )


def test_vm_action_still_works_when_another_job_queued(staff_client, vm):
    """核心回归：DB 里躺着一个排队任务时，其他按钮必须照样能点。"""
    from vmmanager.jobs import queue
    from vmmanager.models import JobKind

    queue.enqueue(JobKind.POWER_ON, payload={"vm_id": vm.pk}, vm=vm, dedupe_key=f"power_on:{vm.pk}")

    response = staff_client.post(f"/vms/{vm.pk}/action/stop/", {})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    # 应告知用户前面还有任务，而不是假装立刻就执行
    assert response.json()["queued_behind"] == 1


def test_vm_action_reports_queued_behind(staff_client, vm):
    from vmmanager.jobs import queue
    from vmmanager.models import JobKind

    for _ in range(2):
        queue.enqueue(JobKind.PROBE_IP, payload={"vm_id": vm.pk}, vm=vm)

    response = staff_client.post(f"/vms/{vm.pk}/action/suspend/", {})

    assert response.json()["queued_behind"] == 2


def test_vm_action_rejects_duplicate_same_action(staff_client, vm):
    """同一个动作连点两下仍然要被去重拦住，否则会堆出一串重复任务。"""
    first = staff_client.post(f"/vms/{vm.pk}/action/start/", {})
    second = staff_client.post(f"/vms/{vm.pk}/action/start/", {})

    assert first.status_code == 200
    assert second.status_code == 409
    assert "已在排队中" in second.json()["error"]


def test_vm_action_respects_pending_limit(staff_client, vm, settings):
    """排队有上限，无限堆叠会让执行顺序彻底不可控。"""
    from vmmanager.jobs import queue
    from vmmanager.models import JobKind

    limit = settings.VMM["MAX_PENDING_PER_VM"]
    for _ in range(limit):
        queue.enqueue(JobKind.PROBE_IP, payload={"vm_id": vm.pk}, vm=vm)

    response = staff_client.post(f"/vms/{vm.pk}/action/suspend/", {})

    assert response.status_code == 429
    assert "超过上限" in response.json()["error"]


def test_dashboard_shows_pending_job_count(staff_client, vm):
    from vmmanager.jobs import queue
    from vmmanager.models import JobKind

    queue.enqueue(JobKind.POWER_ON, payload={"vm_id": vm.pk}, vm=vm)

    body = staff_client.get("/").content.decode()
    assert "个任务" in body
