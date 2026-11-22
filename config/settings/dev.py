"""开发环境：DEBUG 打开，日志往控制台打。"""

from .base import *  # noqa: F401,F403
from .base import conf_bool

DEBUG = True

# 开发时允许局域网访问，方便手机上瞄一眼
ALLOWED_HOSTS = ["*"]

# 开发用 runserver 时静态文件由 django 处理，这里不需要额外配置
INTERNAL_IPS = ["127.0.0.1"]

if conf_bool("VMM_SQL_ECHO", False):
    LOGGING["loggers"]["django.db.backends"] = {"level": "DEBUG"}  # noqa: F405
