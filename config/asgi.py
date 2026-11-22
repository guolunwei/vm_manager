"""ASGI 入口。

当前没有 async 视图，保留这个入口主要是为了以后用 WebSocket 推送任务日志
（替代前端轮询）。
"""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_asgi_application()
