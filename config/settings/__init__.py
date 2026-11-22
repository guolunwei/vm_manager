"""配置包。

默认加载 dev，可用环境变量切换：

    set DJANGO_SETTINGS_MODULE=config.settings.prod

这里不做 ``from .dev import *``，避免 ``config.settings`` 被误当成配置文件时
在无提示的情况下带上 DEBUG=True 之外的行为差异；manage.py 里已显式指定。
"""
