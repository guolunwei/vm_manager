"""数据模型层的行为约束。"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from vmmanager.models import (
    IpLease,
    LeaseKind,
    VirtualMachine,
    hostname_for,
    vm_name_validator,
)


@pytest.mark.parametrize("name", ["vm-1", "CentOS7", "a.b_c-d", "a" * 63])
def test_accepts_valid_names(name):
    vm_name_validator(name)  # 不抛异常即通过


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-leading-dash",
        ".leading-dot",
        "has space",
        "中文名字",
        "a/b",
        "a" * 64,
    ],
)
def test_rejects_invalid_names(name):
    with pytest.raises(ValidationError):
        vm_name_validator(name)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("vm-1", "vm-1"),
        ("My VM", "my-vm"),
        ("Windows 10 x64", "windows-10-x64"),
        ("  Trim  ", "trim"),
        ("中文", "vm"),  # 全被过滤掉时兜底
    ],
)
def test_hostname_for(raw, expected):
    """纳管外部虚拟机时主机名必须合法 —— 不能带空格。"""
    assert hostname_for(raw) == expected


@pytest.mark.django_db
def test_lease_reserve_is_exclusive():
    first = IpLease.reserve("192.168.47.10")
    assert first is not None
    assert first.ip == "192.168.47.10"

    # 同一地址再占一次：没有归属虚拟机，视为复用同一条记录
    again = IpLease.reserve("192.168.47.10")
    assert again is not None and again.pk == first.pk


@pytest.mark.django_db
def test_lease_reserve_rejects_ip_owned_by_other_vm():
    owner = VirtualMachine.objects.create(
        name="vm-a",
        vmx_path=r"D:\VMs\vm-a\vm-a.vmx",
        cpus=1,
        memory_mb=1024,
    )
    other = VirtualMachine.objects.create(
        name="vm-b",
        vmx_path=r"D:\VMs\vm-b\vm-b.vmx",
        cpus=1,
        memory_mb=1024,
    )

    assert IpLease.reserve("192.168.47.20", vm=owner) is not None
    assert IpLease.reserve("192.168.47.20", vm=other) is None, "不能把别人的地址再分一次"


@pytest.mark.django_db
def test_lease_release_frees_ip():
    IpLease.reserve("192.168.47.30", kind=LeaseKind.STATIC)
    IpLease.release("192.168.47.30")
    assert not IpLease.objects.filter(ip="192.168.47.30").exists()


@pytest.mark.django_db
def test_lease_release_is_idempotent():
    IpLease.release("192.168.47.99")  # 不存在也不该报错


@pytest.mark.django_db
def test_vm_name_is_unique_case_insensitively():
    """名称走校验器 + 不区分大小写的唯一约束（Windows 文件系统语义）。"""
    VirtualMachine.objects.create(
        name="vm-x",
        vmx_path=r"D:\VMs\vm-x\vm-x.vmx",
        cpus=1,
        memory_mb=1024,
    )
    with pytest.raises(IntegrityError):
        VirtualMachine.objects.create(
            name="VM-X",
            vmx_path=r"D:\VMs\vm-y\vm-y.vmx",
            cpus=1,
            memory_mb=1024,
        )
