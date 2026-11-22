"""生产环境：关闭 DEBUG，补齐安全响应头。

用 waitress / gunicorn 托管时指定 ``DJANGO_SETTINGS_MODULE=config.settings.prod``。
"""

from .base import *  # noqa: F401,F403
from .base import DATA_DIR, _split_csv, conf, conf_bool

DEBUG = False

# 主机名白名单。两种写法都认：DJANGO_ALLOWED_HOSTS（Django 习惯）和
# VMM_ALLOWED_HOSTS（本项目旧写法），避免升级后配置失效。
ALLOWED_HOSTS = _split_csv(conf("DJANGO_ALLOWED_HOSTS") or conf("VMM_ALLOWED_HOSTS", "127.0.0.1,localhost"))

# 反向代理（nginx / caddy）终止 TLS 时打开
if conf_bool("VMM_TRUST_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = conf_bool("VMM_HTTPS_ONLY", False)
CSRF_COOKIE_SECURE = conf_bool("VMM_HTTPS_ONLY", False)
SECURE_SSL_REDIRECT = conf_bool("VMM_HTTPS_ONLY", False)

# 生产环境把运行日志落盘，方便排查 worker 问题
LOGGING["handlers"]["file"] = {  # noqa: F405
    "class": "logging.handlers.RotatingFileHandler",
    "filename": str(DATA_DIR / "vmmanager.log"),
    "maxBytes": 8 * 1024 * 1024,
    "backupCount": 5,
    "formatter": "simple",
    "encoding": "utf-8",
}
LOGGING["loggers"]["vmmanager"]["handlers"] = ["console", "file"]  # noqa: F405
