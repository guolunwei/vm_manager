"""基础配置：所有环境共享。

配置来源优先级（高 → 低）：

1. 环境变量
2. 项目根目录的 ``.env``
3. 项目根目录的 ``config.ini``（``export KEY="VALUE"`` 形式，兼容原有 vmctl.sh）
4. 代码里的默认值

业务代码统一通过 ``settings.VMM`` 读取，不要在别处直接找 config.ini。
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 配置文件解析
# ---------------------------------------------------------------------------
def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _read_kv_file(path: Path, *, strip_export: bool = False) -> dict[str, str]:
    """读取 ``KEY=VALUE`` 文件；``strip_export`` 用于兼容 shell 风格配置。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if strip_export:
            line = re.sub(r"^export\s+", "", line)
        key, _, value = line.partition("=")
        data[key.strip()] = _unquote(value)
    return data


_FILE_CONFIG: dict[str, str] = {}
_FILE_CONFIG.update(_read_kv_file(BASE_DIR / "config.ini", strip_export=True))
_FILE_CONFIG.update(_read_kv_file(BASE_DIR / ".env"))


def conf(key: str, default: str = "") -> str:
    """按优先级取值。"""
    return os.environ.get(key) or _FILE_CONFIG.get(key) or default


def conf_int(key: str, default: int) -> int:
    try:
        return int(conf(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def conf_bool(key: str, default: bool) -> bool:
    raw = conf(key, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# vmrun 探测
# ---------------------------------------------------------------------------
def _detect_ovftool() -> str:
    """OVF 导出工具（VMware Workstation 自带，也可能随 vSphere 客户端安装）。

    找不到就返回空串——导出功能会优雅降级为「页面上提示未安装」，
    不影响其它功能。
    """
    candidates = [
        # Windows
        r"D:\Applications\VMware\VMware Workstation\ovftool\ovftool.exe",
        r"C:\Program Files (x86)\VMware\VMware Workstation\ovftool\ovftool.exe",
        r"C:\Program Files\VMware\VMware Workstation\ovftool\ovftool.exe",
        r"C:\Program Files\VMware\ovftool\ovftool.exe",
        # Linux / macOS
        "/usr/bin/ovftool",
        "/usr/local/bin/ovftool",
        "/Applications/VMware Fusion.app/Contents/Library/ovftool/ovftool",
    ]
    for item in candidates:
        if Path(item).exists():
            return item
    # 最后碰碰运气：PATH 里有就能用
    found = shutil.which("ovftool")
    return found or ""


def _detect_vmrun() -> str:
    candidates = [
        # Windows
        r"D:\Applications\VMware\VMware Workstation\vmrun.exe",
        r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
        r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
        r"C:\Program Files (x86)\VMware\VMware Player\vmrun.exe",
        r"C:\Program Files\VMware\VMware Player\vmrun.exe",
        # Linux / macOS
        "/usr/bin/vmrun",
        "/usr/local/bin/vmrun",
        "/Applications/VMware Fusion.app/Contents/Library/vmrun",
    ]
    for item in candidates:
        if Path(item).exists():
            return item
    return "vmrun"


def _detect_vm_base_dir() -> str:
    """猜虚拟机根目录，猜不到就用用户目录下的 Virtual Machines。"""
    candidates = [
        r"D:\Virtual Machines",
        r"C:\Virtual Machines",
        Path.home() / "Virtual Machines",
        Path.home() / "Documents" / "Virtual Machines",
    ]
    for item in candidates:
        if Path(str(item)).is_dir():
            return str(item)
    return str(Path.home() / "Virtual Machines")


# ---------------------------------------------------------------------------
# 业务配置
# ---------------------------------------------------------------------------
VMM = {
    # 虚拟机存放根目录，「同步磁盘」会扫描它下面一层子目录的 .vmx
    "BASE_DIR": conf("VMM_BASE_DIR", conf("BASE_DIR", _detect_vm_base_dir())),
    "VMRUN": conf("VMM_VMRUN", conf("VMRUN", _detect_vmrun())),
    # OVF 导出：ovftool 路径（找不到 = 导出功能显示为不可用）与导出目录
    "OVFTOOL": conf("VMM_OVFTOOL", conf("OVFTOOL", _detect_ovftool())),
    "EXPORT_DIR": conf("VMM_EXPORT_DIR", conf("EXPORT_DIR", "")),
    # 磁盘扩容工具，与 vmrun 同目录（见 services/disk.py）
    "VDISKMANAGER": conf("VMM_VDISKMANAGER", conf("VDISKMANAGER", "")),
    # 默认模板（bootstrap 首次初始化时使用）
    "TEMPLATE_VMX": conf("VMM_TEMPLATE_VMX", conf("SRC_VMX", "")),
    "SNAPSHOT": conf("VMM_SNAPSHOT", conf("SNAPSHOT", "")),
    "GUEST_USER": conf("VMM_GUEST_USER", "root"),
    "GUEST_PASSWORD": conf("VMM_GUEST_PASSWORD", conf("ROOTPASS", "")),
    "GUEST_CONNECTION": conf("VMM_GUEST_CONNECTION", conf("CONNECTION", "eth0")),
    # 下发到客户机 authorized_keys 的公钥；留空则自动使用运行账号的 ~/.ssh/*.pub
    "SSH_PUBKEY": conf("VMM_SSH_PUBKEY", conf("SSH_PUBKEY", "")),
    # 网络三件套：config.ini 沿用裸名（GATEWAY / DNS / NETMASK_PREFIX），
    # 环境变量优先用带前缀的 VMM_*，两种都要认，否则用户按文档填了不生效。
    "NETMASK_PREFIX": conf_int("VMM_NETMASK_PREFIX", conf_int("NETMASK_PREFIX", 24)),
    "GATEWAY": conf("VMM_GATEWAY", conf("GATEWAY", "")),
    "DNS": conf("VMM_DNS", conf("DNS", "")),
    "DEFAULT_CPUS": conf_int("VMM_DEFAULT_CPUS", 2),
    "DEFAULT_MEMORY_MB": conf_int("VMM_DEFAULT_MEMORY_MB", 4096),
    # 单个任务的硬超时（秒），worker 用它兜底
    "JOB_TIMEOUT": conf_int("VMM_JOB_TIMEOUT", 1800),
    # 等待 Guest 可执行命令的最长时间（秒）
    "TOOLS_WAIT": conf_int("VMM_TOOLS_WAIT", 360),
    # worker 心跳间隔与判定失联的阈值（秒）
    "WORKER_HEARTBEAT": conf_int("VMM_WORKER_HEARTBEAT", 10),
    "WORKER_STALE_AFTER": conf_int("VMM_WORKER_STALE_AFTER", 120),
    # 同一台机器允许排队的任务上限。动作可以排队（按点击顺序执行），
    # 但不设上限的话，狂点几下就能堆出几十个任务，user 也理不清顺序。
    "MAX_PENDING_PER_VM": conf_int("VMM_MAX_PENDING_PER_VM", 6),
    # 强制关机前等待软关机的秒数
    "SOFT_STOP_GRACE": conf_int("VMM_SOFT_STOP_GRACE", 60),
    # IP 探测：是否允许扫描整个网段
    "SCAN_ENABLED": conf_bool("VMM_SCAN_ENABLED", True),
    "SCAN_TIMEOUT_MS": conf_int("VMM_SCAN_TIMEOUT_MS", 500),
    # noVNC 控制台：websockify 代理监听端口 / token 文件目录。
    # 代理只做「凭 token 转发」，token 由 Django 签发（见 services/vnc.py）。
    "VNC_PROXY_PORT": conf_int("VMM_VNC_PROXY_PORT", 6080),
    "VNC_PROXY_BIND": conf("VMM_VNC_PROXY_BIND", "0.0.0.0"),
    "VNC_TOKEN_TTL": conf_int("VMM_VNC_TOKEN_TTL", 300),
    # NAT 转发 / DHCP 静态绑定的配置文件路径（留空自动探测，见 services/natconf.py）
    "NAT_CONF": conf("VMM_NAT_CONF", conf("NAT_CONF", "")),
    "DHCP_CONF": conf("VMM_DHCP_CONF", conf("DHCP_CONF", "")),
    # 监控采集（Guest 内 /proc 采样，见 services/collector.py）。
    # 默认关闭：每轮要对每台运行中的机器多跑几次 vmrun，按需打开。
    "METRICS_ENABLED": conf_bool("VMM_METRICS_ENABLED", conf_bool("METRICS_ENABLED", False)),
    "METRICS_INTERVAL": conf_int("VMM_METRICS_INTERVAL", conf_int("METRICS_INTERVAL", 300)),
    "METRICS_RETENTION_DAYS": conf_int("VMM_METRICS_RETENTION_DAYS", conf_int("METRICS_RETENTION_DAYS", 7)),
    # 任务终态通知（worker 推送，见 services/notify.py）。默认只在失败时通知。
    "NOTIFY_ON_FAILED": conf_bool("VMM_NOTIFY_ON_FAILED", conf_bool("NOTIFY_ON_FAILED", True)),
    "NOTIFY_ON_SUCCESS": conf_bool("VMM_NOTIFY_ON_SUCCESS", conf_bool("NOTIFY_ON_SUCCESS", False)),
    "NOTIFY_ON_CANCELED": conf_bool("VMM_NOTIFY_ON_CANCELED", conf_bool("NOTIFY_ON_CANCELED", False)),
    "NOTIFY_WECHAT_WEBHOOK": conf("VMM_NOTIFY_WECHAT_WEBHOOK", conf("NOTIFY_WECHAT_WEBHOOK", "")),
    "NOTIFY_DINGTALK_WEBHOOK": conf("VMM_NOTIFY_DINGTALK_WEBHOOK", conf("NOTIFY_DINGTALK_WEBHOOK", "")),
    "NOTIFY_EMAIL_TO": conf("VMM_NOTIFY_EMAIL_TO", conf("NOTIFY_EMAIL_TO", "")),
}

# SMTP（发送通知邮件用；不配 EMAIL_HOST 就不发邮件，webhook 不受影响）
EMAIL_HOST = conf("VMM_EMAIL_HOST", conf("EMAIL_HOST", ""))
EMAIL_PORT = conf_int("VMM_EMAIL_PORT", conf_int("EMAIL_PORT", 25))
EMAIL_HOST_USER = conf("VMM_EMAIL_HOST_USER", conf("EMAIL_HOST_USER", ""))
EMAIL_HOST_PASSWORD = conf("VMM_EMAIL_HOST_PASSWORD", conf("EMAIL_HOST_PASSWORD", ""))
EMAIL_USE_TLS = conf_bool("VMM_EMAIL_USE_TLS", conf_bool("EMAIL_USE_TLS", False))
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = conf("VMM_EMAIL_FROM", conf("DEFAULT_FROM_EMAIL", "vmmanager@localhost"))

# 是否允许普通用户自助创建虚拟机（关闭后只有 staff 能创建）
ALLOW_SELF_SERVICE = conf_bool("VMM_ALLOW_SELF_SERVICE", True)


# ---------------------------------------------------------------------------
# Django 基础
# ---------------------------------------------------------------------------
def _secret_key() -> str:
    env_key = conf("VMM_SECRET_KEY", "")
    if env_key:
        return env_key
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(48)
    key_file.write_text(key, encoding="utf-8")
    return key


SECRET_KEY = _secret_key()

DEBUG = conf_bool("VMM_DEBUG", True)

ALLOWED_HOSTS = _split_csv(conf("VMM_ALLOWED_HOSTS", "127.0.0.1,localhost")) or ["*"]
CSRF_TRUSTED_ORIGINS = _split_csv(conf("VMM_CSRF_TRUSTED_ORIGINS", ""))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "rest_framework.authtoken",
    "vmmanager",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # 生产环境由 Django 自己发静态文件：DEBUG=False 时它不再直供 static/，
    # 而本项目没有前端构建步骤、也没打算强绑 nginx，WhiteNoise 是最小的解法。
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "vmmanager.web.middleware.AuditMiddleware",
]

# 说明：不使用 django.contrib.auth.middleware.LoginRequiredMiddleware。
# Django 6 起它只认视图上的 login_not_required 装饰器，不再自动放行 LOGIN_URL，
# 很容易把登录页本身也拦掉；本项目的每个视图都已显式 @login_required，
# 权限来源单一，更好排查。

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "vmmanager.web.context_processors.site_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {
            # worker 与 web 进程会同时写库，给足等待时间
            "timeout": 30,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = conf("VMM_TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = DATA_DIR / "staticfiles"

# 生产环境用 collectstatic 的产物 + WhiteNoise 压缩。
# 不用带 manifest 的 storage：那会在漏跑 collectstatic 时直接 500，
# 本项目更希望「样式没更新」而不是「整站打不开」。
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # API 一律要求认证 —— 没有「匿名可读」的开关，避免误暴露
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s"},
        "verbose": {"format": "%(asctime)s %(levelname)-7s %(name)s %(funcName)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "vmmanager": {"handlers": ["console"], "level": conf("VMM_LOG_LEVEL", "INFO")},
        "django.db.backends": {"level": "WARNING"},
        "django.request": {"handlers": ["console"], "level": "ERROR"},
    },
}
