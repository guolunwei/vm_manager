#!/usr/bin/env python
"""Django 命令行入口。

默认加载开发配置；生产部署请显式指定：

    set DJANGO_SETTINGS_MODULE=config.settings.prod
"""

import os
import sys
from pathlib import Path


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError("无法导入 Django。请确认已激活虚拟环境并安装依赖：pip install -e .") from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
