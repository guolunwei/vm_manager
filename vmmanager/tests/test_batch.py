"""批量创建的校验与入队。

批量操作最怕「一行错、全批废」，所以这里重点测：
**坏行被单独挑出来，好行照常提交。**
"""

from __future__ import annotations

import pytest

from vmmanager.models import Job, JobKind, VirtualMachine, VmTemplate
from vmmanager.services import batch

pytestmark = pytest.mark.django_db


@pytest.fixture
def template(db, tmp_path):
    vmx_file = tmp_path / "base.vmx"
    vmx_file.write_text('displayName = "base"\r\n', encoding="utf-8")
    return VmTemplate.objects.create(
        name="tpl-batch",
        vmx_path=str(vmx_file),
        guest_user="root",
        default_cpus=2,
        default_memory_mb=2048,
    )


def rows(*names, **extra):
    return [
        {"name": name, "cpus": extra.get("cpus", 2), "memory_mb": extra.get("memory_mb", 2048)}
        for name in names
    ]


def test_valid_rows_pass(template):
    outcome = batch.validate_rows(rows("web-01", "web-02"), default_cpus=2, default_memory_mb=2048)

    assert outcome.ok
    assert [r.name for r in outcome.rows] == ["web-01", "web-02"]


def test_empty_name_is_rejected_per_row(template):
    outcome = batch.validate_rows(
        [{"name": "web-01"}, {"name": "  "}, {"name": "web-03"}],
        default_cpus=2,
        default_memory_mb=2048,
    )

    assert len(outcome.rows) == 2  # 好行照样保留
    assert len(outcome.errors) == 1
    assert outcome.errors[0]["index"] == 2


def test_duplicate_inside_one_batch_is_rejected(template):
    """同一批里自己重名，必须拦住——否则第二台会卡在目录已存在。"""
    outcome = batch.validate_rows(rows("web-01", "WEB-01"), default_cpus=2, default_memory_mb=2048)

    assert len(outcome.rows) == 1
    assert "名称重复" in outcome.errors[0]["message"]


def test_duplicate_with_existing_vm_is_rejected(template):
    VirtualMachine.objects.create(
        name="web-01",
        vmx_path=r"D:\VMs\web-01\web-01.vmx",
        cpus=2,
        memory_mb=2048,
    )

    outcome = batch.validate_rows(rows("WEB-01"), default_cpus=2, default_memory_mb=2048)

    assert not outcome.rows
    assert "已存在同名" in outcome.errors[0]["message"]


def test_invalid_name_characters_rejected(template):
    outcome = batch.validate_rows(rows("bad name!"), default_cpus=2, default_memory_mb=2048)

    assert not outcome.rows
    assert "只能包含" in outcome.errors[0]["message"]


def test_cpu_and_memory_bounds(template):
    outcome = batch.validate_rows(
        [{"name": "a", "cpus": 0, "memory_mb": 2048}, {"name": "b", "cpus": 2, "memory_mb": 100}],
        default_cpus=2,
        default_memory_mb=2048,
    )

    assert not outcome.rows
    assert len(outcome.errors) == 2


def test_bad_ip_rejected_and_duplicate_ip_rejected(template):
    outcome = batch.validate_rows(
        [
            {"name": "a", "ip": "not-an-ip"},
            {"name": "b", "ip": "192.168.47.50"},
            {"name": "c", "ip": "192.168.47.50"},
        ],
        default_cpus=2,
        default_memory_mb=2048,
    )

    assert [r.name for r in outcome.rows] == ["b"]
    messages = [e["message"] for e in outcome.errors]
    assert any("格式" in m for m in messages)
    assert any("IP 重复" in m for m in messages)


def test_occupied_ip_rejected(template):
    from vmmanager.models import IpLease

    IpLease.reserve("192.168.47.60", note="测试占用")

    outcome = batch.validate_rows(
        [{"name": "a", "ip": "192.168.47.60"}],
        default_cpus=2,
        default_memory_mb=2048,
    )

    assert not outcome.rows
    assert "已被占用" in outcome.errors[0]["message"]


def test_row_limit_enforced(template):
    too_many = rows(*[f"vm-{i:02d}" for i in range(batch.MAX_ROWS + 1)])

    outcome = batch.validate_rows(too_many, default_cpus=2, default_memory_mb=2048)

    assert not outcome.rows
    assert "最多" in outcome.errors[0]["message"]


def test_empty_rows_rejected(template):
    outcome = batch.validate_rows([], default_cpus=2, default_memory_mb=2048)

    assert not outcome.rows
    assert outcome.errors


def test_submit_enqueues_one_job_per_row(template):
    user = (
        __import__("django.contrib.auth", fromlist=["get_user_model"])
        .get_user_model()
        .objects.create_user(
            username="op",
            password="pw-op-1",
        )
    )
    outcome = batch.validate_rows(rows("web-01", "web-02"), default_cpus=2, default_memory_mb=2048)

    result = batch.submit(outcome.rows, template=template, user=user)

    assert len(result["jobs"]) == 2
    assert not result["errors"]
    assert Job.objects.filter(kind=JobKind.PROVISION).count() == 2


def test_submit_reports_duplicate_job_instead_of_crashing(template):
    """同一个名字重复提交（比如连点两次）应返回行级错误，而不是 500。"""
    user = (
        __import__("django.contrib.auth", fromlist=["get_user_model"])
        .get_user_model()
        .objects.create_user(
            username="op2",
            password="pw-op-2",
        )
    )
    outcome = batch.validate_rows(rows("web-01"), default_cpus=2, default_memory_mb=2048)
    batch.submit(outcome.rows, template=template, user=user)

    again = batch.submit(outcome.rows, template=template, user=user)

    assert not again["jobs"]
    assert "已在排队中" in again["errors"][0]["message"]


# ---------------------------------------------------------------------------
# 行级模板 / 克隆方式覆盖
# ---------------------------------------------------------------------------
@pytest.fixture
def second_template(db, tmp_path):
    vmx_file = tmp_path / "other.vmx"
    vmx_file.write_text('displayName = "other"\r\n', encoding="utf-8")
    return VmTemplate.objects.create(
        name="tpl-other",
        vmx_path=str(vmx_file),
        guest_user="root",
        default_cpus=4,
        default_memory_mb=4096,
    )


def test_row_level_template_override(template, second_template):
    outcome = batch.validate_rows(
        [
            {"name": "web-01", "template_id": second_template.pk},
            {"name": "web-02"},
        ],
        default_cpus=2,
        default_memory_mb=2048,
        template_ids={template.pk, second_template.pk},
    )

    assert outcome.ok
    assert outcome.rows[0].template_id == second_template.pk
    assert outcome.rows[1].template_id is None


def test_row_level_unknown_template_rejected(template, second_template):
    outcome = batch.validate_rows(
        [{"name": "web-01", "template_id": 99999}],
        default_cpus=2,
        default_memory_mb=2048,
        template_ids={template.pk, second_template.pk},
    )

    assert not outcome.rows
    assert "模板" in outcome.errors[0]["message"]


def test_submit_uses_row_level_template(template, second_template):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="op3", password="pw-qq-9")
    outcome = batch.validate_rows(
        [
            {"name": "web-01", "template_id": second_template.pk, "clone_type": "full"},
            {"name": "web-02", "clone_type": "bogus"},
        ],
        default_cpus=2,
        default_memory_mb=2048,
        template_ids={template.pk, second_template.pk},
    )

    result = batch.submit(
        outcome.rows,
        template=template,
        user=user,
        clone_type="linked",
        templates={template.pk: template, second_template.pk: second_template},
    )

    assert len(result["jobs"]) == 2
    payloads = {job["name"]: Job.objects.get(pk=job["job_id"]).payload for job in result["jobs"]}
    assert payloads["web-01"]["template_id"] == second_template.pk
    assert payloads["web-01"]["clone_type"] == "full"
    # 无效 clone_type 回落到公共配置 linked
    assert payloads["web-02"]["template_id"] == template.pk
    assert payloads["web-02"]["clone_type"] == "linked"
