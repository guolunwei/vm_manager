"""审计中间件。

只做兜底：把「非 GET 且成功」的请求记一条。视图里已经精细记录过的
（``AuditEvent.record(request=request)`` 会置 ``request._audited``）不重复记。
"""

from __future__ import annotations

import logging

from vmmanager.models import AuditAction, AuditEvent

logger = logging.getLogger("vmmanager.audit")

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SKIP_PREFIXES = ("/static/", "/favicon.ico", "/api/jobs/", "/api/operations/")


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._maybe_record(request, response)
        except Exception:  # 审计失败不能影响业务
            logger.exception("记录审计事件失败")
        return response

    def _maybe_record(self, request, response) -> None:
        if request.method not in _WRITE_METHODS:
            return
        if getattr(request, "_audited", False):
            return
        if response.status_code >= 400:
            return
        if any(request.path.startswith(prefix) for prefix in _SKIP_PREFIXES):
            return
        if not request.path.startswith("/"):
            return

        user = getattr(request, "user", None)
        # 登录页的 POST 由视图自己记（要带成功/失败结果）
        if request.path.rstrip("/").endswith("login"):
            return

        AuditEvent.record(
            actor=user if getattr(user, "is_authenticated", False) else None,
            action=AuditAction.API if request.path.startswith("/api/") else AuditAction.SETTINGS,
            target_label=request.path,
            detail={"method": request.method, "status": response.status_code},
            request=request,
        )
