"""任务注册表：kind → handler。

handler 签名统一为 ``(job, ctx) -> dict | None``，返回值会存进 ``job.result``。
"""

from __future__ import annotations

from collections.abc import Callable

from vmmanager.models import Job

from .base import JobContext

Handler = Callable[[Job, JobContext], dict | None]

_REGISTRY: dict[str, Handler] = {}


def register(kind: str):
    """装饰器：把函数登记为某类任务的处理器。"""

    def decorator(func: Handler) -> Handler:
        if kind in _REGISTRY:
            raise RuntimeError(f"任务类型 {kind} 已注册")
        _REGISTRY[kind] = func
        return func

    return decorator


def get_handler(kind: str) -> Handler | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def load_handlers() -> None:
    """导入 handlers 模块，触发所有 @register。"""
    from . import handlers  # noqa: F401
