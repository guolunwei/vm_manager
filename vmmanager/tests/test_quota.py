"""多用户配额：校验逻辑、创建入口拦截、设置页管理。"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from vmmanager.models import (
    QuotaExceeded,
    UserQuota,
    VmTemplate,
    check_quota_for_create,
    usage_for,
)
from vmmanager.services import batch

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    return User.objects.create_user(username="tenant", password="pw-12345678")


@pytest.fixture
def staff():
    return User.objects.create_user(username="boss", password="pw-12345678", is_staff=True)


@pytest.fixture
def template():
    return VmTemplate.objects.create(
        name="tpl",
        vmx_path=r"D:\VMs\tpl\tpl.vmx",
        os_family="linux",
        guest_user="root",
        guest_password="x",
    )


# ---------------------------------------------------------------------------
# check_quota_for_create
# ---------------------------------------------------------------------------
def test_no_quota_record_allows_anything(owner):
    check_quota_for_create(owner, count=99, memory_mb_total=999999)


def test_count_quota_blocks(owner):
    UserQuota.objects.create(user=owner, max_vms=2)
    check_quota_for_create(owner, count=2, memory_mb_total=0)
    with pytest.raises(QuotaExceeded, match="数量配额"):
        check_quota_for_create(owner, count=3, memory_mb_total=0)


def test_count_quota_accounts_existing_vms(owner, template):
    UserQuota.objects.create(user=owner, max_vms=1)
    from vmmanager.models import VirtualMachine

    VirtualMachine.objects.create(
        name="already", vmx_path=r"D:\VMs\already\already.vmx", owner=owner, memory_mb=512
    )
    usage = usage_for(owner)
    assert usage["vms"] == 1
    with pytest.raises(QuotaExceeded):
        check_quota_for_create(owner, count=1, memory_mb_total=0)


def test_memory_quota_blocks(owner):
    UserQuota.objects.create(user=owner, max_memory_mb=8192)
    check_quota_for_create(owner, count=1, memory_mb_total=8192)
    # 注意：这里没有真建机器，usage 始终为 0，所以第二次用翻倍的新增量来触发上限
    with pytest.raises(QuotaExceeded, match="内存总量配额"):
        check_quota_for_create(owner, count=2, memory_mb_total=16384)


def test_staff_is_exempt(staff):
    UserQuota.objects.create(user=staff, max_vms=0, max_memory_mb=1024)
    check_quota_for_create(staff, count=50, memory_mb_total=999999)


# ---------------------------------------------------------------------------
# batch.submit 整批拦截
# ---------------------------------------------------------------------------
def test_batch_submit_rejects_over_quota(owner, template):
    UserQuota.objects.create(user=owner, max_vms=1)
    rows = [
        batch.Row(index=0, name="q-1", cpus=1, memory_mb=1024),
        batch.Row(index=1, name="q-2", cpus=1, memory_mb=1024),
    ]
    with pytest.raises(QuotaExceeded):
        batch.submit(rows, template=template, user=owner)


def test_batch_submit_passes_within_quota(owner, template, monkeypatch):
    UserQuota.objects.create(user=owner, max_vms=5, max_memory_mb=8192)
    rows = [batch.Row(index=0, name="q-ok", cpus=1, memory_mb=1024)]

    # 入队本身会被队列层拦（没有真 vmrun）——这里只关心配额校验放行，
    # 所以把 enqueue 替换成假对象
    class FakeJob:
        pk = 1

    monkeypatch.setattr(batch.queue, "enqueue", lambda *a, **kw: FakeJob(), raising=False)
    result = batch.submit(rows, template=template, user=owner)
    assert result["jobs"]
    assert not result["errors"]


# ---------------------------------------------------------------------------
# API / 设置页
# ---------------------------------------------------------------------------
def test_api_create_rejects_over_quota(client, owner, template):
    UserQuota.objects.create(user=owner, max_vms=0, max_memory_mb=1024)
    client.force_login(owner)
    response = client.post(
        "/api/v1/vms/",
        {"name": "over-quota", "template": template.pk, "memory_mb": 4096},
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "内存总量配额" in response.json()["detail"]


def test_api_create_allows_within_quota(client, owner, template):
    UserQuota.objects.create(user=owner, max_vms=5, max_memory_mb=8192)
    client.force_login(owner)
    response = client.post(
        "/api/v1/vms/",
        {"name": "ok-vm", "template": template.pk, "memory_mb": 4096},
        content_type="application/json",
    )
    assert response.status_code == 202


def test_settings_page_saves_quota(client, owner, staff):
    client.force_login(staff)
    response = client.post(
        reverse("settings"),
        {"action": "quota", "user_id": owner.pk, "max_vms": "3", "max_memory_mb": "16384"},
    )
    assert response.status_code == 302
    quota = UserQuota.objects.get(user=owner)
    assert quota.max_vms == 3
    assert quota.max_memory_mb == 16384


def test_settings_quota_blank_means_unlimited(client, owner, staff):
    UserQuota.objects.create(user=owner, max_vms=5, max_memory_mb=8192)
    client.force_login(staff)
    client.post(
        reverse("settings"),
        {"action": "quota", "user_id": owner.pk, "max_vms": "", "max_memory_mb": "0"},
    )
    quota = UserQuota.objects.get(user=owner)
    assert quota.max_vms == 0
    assert quota.max_memory_mb == 0


def test_settings_quota_denied_for_non_staff(client, owner):
    client.force_login(owner)
    response = client.post(
        reverse("settings"),
        {"action": "quota", "user_id": owner.pk, "max_vms": "99"},
    )
    assert response.status_code == 302
    assert not UserQuota.objects.filter(user=owner).exists()
