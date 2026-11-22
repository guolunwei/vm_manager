"""领域模型。

业务代码统一 ``from vmmanager.models import VirtualMachine`` 这样引用，
不要直接依赖具体子模块，方便以后拆分。
"""

from .audit import AuditAction, AuditEvent
from .base import TimeStampedModel
from .catalog import Flavor, NetworkMode, OsFamily, VmTemplate
from .job import Job, JobKind, JobStatus, Worker
from .machine import (
    CloneType,
    PowerState,
    Snapshot,
    VirtualMachine,
    VmMetric,
    hostname_for,
    vm_name_validator,
)
from .network import IpLease, LeaseKind

__all__ = [
    "AuditAction",
    "AuditEvent",
    "CloneType",
    "Flavor",
    "IpLease",
    "Job",
    "JobKind",
    "JobStatus",
    "LeaseKind",
    "NetworkMode",
    "OsFamily",
    "PowerState",
    "Snapshot",
    "TimeStampedModel",
    "VirtualMachine",
    "VmMetric",
    "VmTemplate",
    "Worker",
    "hostname_for",
    "vm_name_validator",
]
