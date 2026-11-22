"""REST API 的契约测试。

只跑不需要 VMware 的端点；任何会真正操作虚拟机的 POST 都不在这里测。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from vmmanager.models import Flavor, VirtualMachine, VmTemplate

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="tester",
        password="pw-for-test-123",
    )


@pytest.fixture
def client(user):
    api = APIClient()
    api.force_authenticate(user=user)
    return api


def test_token_endpoint_issues_token(db):
    """脚本/CI 需要用账号密码换 Token，不能只支持浏览器会话。"""
    get_user_model().objects.create_user(username="t1", password="pw-1")
    response = APIClient().post(
        "/api/v1/auth/token/",
        {"username": "t1", "password": "pw-1"},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["token"]


def test_token_endpoint_rejects_bad_password(db):
    get_user_model().objects.create_user(username="t3", password="right")
    api = APIClient()
    response = api.post(
        "/api/v1/auth/token/",
        {"username": "t3", "password": "wrong"},
        format="json",
    )
    assert response.status_code == 400


def test_anonymous_is_rejected(db):
    """没有「匿名可读」的开关，未认证一律 401/403。"""
    response = APIClient().get("/api/v1/vms/")
    assert response.status_code in (401, 403)


def test_list_vms(client):
    VirtualMachine.objects.create(
        name="vm-a",
        vmx_path=r"D:\VMs\vm-a\vm-a.vmx",
        cpus=2,
        memory_mb=4096,
    )
    response = client.get("/api/v1/vms/")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["name"] == "vm-a"
    assert body["results"][0]["spec_display"] == "2C / 4 GB"


def test_list_templates(client):
    VmTemplate.objects.create(name="tpl", vmx_path=r"D:\VMs\tpl\tpl.vmx")
    response = client.get("/api/v1/templates/")
    assert response.status_code == 200
    assert response.json()["results"][0]["name"] == "tpl"


def test_list_flavors(client):
    Flavor.objects.create(name="2C4G", cpus=2, memory_mb=4096)
    response = client.get("/api/v1/flavors/")
    assert response.status_code == 200
    assert response.json()["results"][0]["name"] == "2C4G"


def test_host_info(client):
    response = client.get("/api/v1/host/")
    assert response.status_code == 200
    body = response.json()
    for key in ("cpu_percent", "memory_total_mb", "vm_count", "base_dir", "vmrun"):
        assert key in body


def test_job_log_incremental_offset(client):
    """前端靠 ``offset`` 增量拉日志，返回的新 offset 必须单调递增。"""
    from vmmanager.jobs import queue
    from vmmanager.models import JobKind

    job = queue.enqueue(JobKind.PROBE_IP, label="探测")
    job.append_log("第一行")

    first = client.get(f"/api/v1/jobs/{job.pk}/log/?offset=0").json()
    assert "第一行" in first["chunk"]
    offset = first["offset"]
    assert offset == len(job.log)

    second = client.get(f"/api/v1/jobs/{job.pk}/log/?offset={offset}").json()
    assert second["chunk"] == ""
    assert second["offset"] == offset


def test_create_vm_requires_name(client):
    response = client.post("/api/v1/vms/", {"template": 1}, format="json")
    assert response.status_code == 400
