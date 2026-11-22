"""WSGI 入口。

生产环境用 waitress（Windows）或 gunicorn（Linux/macOS）托管：

    waitress-serve --listen=0.0.0.0:8000 config.wsgi:application
    gunicorn config.wsgi:application -b 0.0.0.0:8000
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()
