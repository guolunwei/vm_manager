"""模板全局变量。"""

from __future__ import annotations

from django.conf import settings

from vmmanager.models import Job, JobStatus


def site_context(request) -> dict:
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"site_name": "VM Manager", "app_version": "0.2.0"}

    active_jobs = Job.objects.exclude(status__in=JobStatus.finished())
    return {
        "site_name": "VM Manager",
        "app_version": "0.2.0",
        "vm_base_dir": settings.VMM.get("BASE_DIR", ""),
        "active_job_count": active_jobs.count(),
        "running_job": active_jobs.filter(status=JobStatus.RUNNING).order_by("id").first(),
    }
