"""审计事件：谁在什么时候对什么做了什么。"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from .base import TimeStampedModel


class AuditAction(models.TextChoices):
    LOGIN = "login", "登录"
    LOGOUT = "logout", "登出"
    CREATE = "create", "创建"
    DELETE = "delete", "删除"
    POWER = "power", "电源操作"
    SNAPSHOT = "snapshot", "快照"
    RECONFIGURE = "reconfigure", "重新配置"
    NETWORK = "network", "网络变更"
    SETTINGS = "settings", "设置变更"
    API = "api", "API 调用"


class AuditEvent(TimeStampedModel):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    actor_label = models.CharField("操作人名称", max_length=150, blank=True, default="")
    action = models.CharField("动作", max_length=16, choices=AuditAction.choices, db_index=True)

    target_type = models.CharField("目标类型", max_length=32, blank=True, default="")
    target_id = models.CharField("目标 ID", max_length=64, blank=True, default="")
    target_label = models.CharField("目标标签", max_length=128, blank=True, default="")

    detail = models.JSONField("详情", default=dict, blank=True)
    source_ip = models.GenericIPAddressField("来源 IP", null=True, blank=True)
    user_agent = models.CharField("User-Agent", max_length=255, blank=True, default="")
    path = models.CharField("请求路径", max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "审计事件"
        verbose_name_plural = "审计事件"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_label} {self.get_action_display()} {self.target_label}"

    @classmethod
    def record(
        cls,
        *,
        actor=None,
        action: str,
        target=None,
        target_label: str = "",
        detail: dict | None = None,
        request=None,
    ) -> AuditEvent:
        """记录一条审计。

        ``request`` 传入时自动带上来源 IP 与 UA；从任务线程里调用时传 None。
        """
        source_ip = None
        user_agent = ""
        path = ""
        if request is not None:
            source_ip = _client_ip(request)
            user_agent = (request.META.get("HTTP_USER_AGENT") or "")[:255]
            path = (request.path or "")[:255]

        if actor is None and request is not None and getattr(request, "user", None):
            if request.user.is_authenticated:
                actor = request.user

        return cls.objects.create(
            actor=actor if getattr(actor, "pk", None) else None,
            actor_label=getattr(actor, "username", "") or "系统",
            action=action,
            target_type=target.__class__.__name__ if target is not None else "",
            target_id=str(getattr(target, "pk", "") or ""),
            target_label=(target_label or str(target or ""))[:128],
            detail=detail or {},
            source_ip=source_ip,
            user_agent=user_agent,
            path=path,
        )


def _client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None
