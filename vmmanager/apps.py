from __future__ import annotations

from django.apps import AppConfig


class VmmanagerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vmmanager"
    verbose_name = "虚拟机管理"

    def ready(self) -> None:
        # 注册信号（目前没有，占位以便以后扩展）
        from . import signals  # noqa: F401
