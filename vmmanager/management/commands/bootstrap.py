"""首次初始化：管理员、默认模板、常用规格。

幂等，可以反复执行。
"""

from __future__ import annotations

import os
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from vmmanager.models import (
    Flavor,
    NetworkMode,
    OsFamily,
    VirtualMachine,
    VmTemplate,
)
from vmmanager.services import network, vmx

DEFAULT_FLAVORS = [
    ("1C1G", 1, 1024, "轻量测试", 10),
    ("1C2G", 1, 2048, "最低配置", 20),
    ("2C4G", 2, 4096, "日常开发", 30),
    ("4C8G", 4, 8192, "中等负载", 40),
    ("8C16G", 8, 16384, "重负载", 50),
    ("16C32G", 16, 32768, "数据库 / 构建", 60),
]


class Command(BaseCommand):
    help = "初始化管理员账号、默认模板与常用规格"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", default="", help="留空则随机生成并打印一次")
        parser.add_argument("--no-flavors", action="store_true", help="跳过创建默认规格")

    def handle(self, *args, **options):
        self._ensure_admin(options["username"], options["password"])
        if not options["no_flavors"]:
            self._ensure_flavors()
        self._ensure_template()
        self.stdout.write(self.style.SUCCESS("初始化完成"))

    # -- 管理员 ------------------------------------------------------------
    def _ensure_admin(self, username: str, password: str) -> None:
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"管理员 {username} 已存在，跳过")
            return

        password = password or secrets.token_urlsafe(9)
        User.objects.create_superuser(username=username, email="", password=password)
        self.stdout.write(self.style.WARNING("=" * 58))
        self.stdout.write(self.style.WARNING(f"  管理员已创建：{username}"))
        self.stdout.write(self.style.WARNING(f"  初始密码：{password}"))
        self.stdout.write(self.style.WARNING("  首次登录后请立即用 changepassword 修改"))
        self.stdout.write(self.style.WARNING("=" * 58))

    # -- 规格 --------------------------------------------------------------
    def _ensure_flavors(self) -> None:
        created = 0
        for name, cpus, mem, desc, order in DEFAULT_FLAVORS:
            defaults = {"cpus": cpus, "memory_mb": mem, "description": desc, "sort_order": order}
            _, is_new = Flavor.objects.get_or_create(name=name, defaults=defaults)
            if is_new:
                created += 1
        if created:
            self.stdout.write(f"已创建 {created} 个规格")

    # -- 模板 --------------------------------------------------------------
    def _ensure_template(self) -> None:
        vmx_path = (settings.VMM.get("TEMPLATE_VMX") or "").strip()
        if not vmx_path or not os.path.isfile(vmx_path):
            self.stdout.write(
                self.style.WARNING(
                    "未配置模板 vmx（config.ini 中的 VMM_TEMPLATE_VMX / SRC_VMX），请在设置页手动添加模板"
                )
            )
            return

        name = os.path.basename(os.path.dirname(vmx_path)) or "default"
        if VmTemplate.objects.filter(name=name).exists():
            return

        with transaction.atomic():
            # 已在虚拟机清单里的，从清单移除，避免一台机器同时是模板又是实例
            VirtualMachine.objects.filter(vmx_path__iexact=vmx_path).delete()

            info = {}
            try:
                info = vmx.summary(vmx_path)
            except Exception:
                pass

            # 连接方式决定用哪个 VMnet，进而能推断网关、DNS 和前缀长度 ——
            # 这几项让用户手填很容易出错（VMnet1 是 .47 网段还是 .17 网段？），
            # 从 VMware 自己的 vmnetdhcp.conf 里读最准。
            connection = "hostonly"
            for nic in info.get("nics") or []:
                if nic.get("index") == 0 and nic.get("connection"):
                    connection = nic["connection"]
                    break
            gateway = (settings.VMM.get("GATEWAY") or "").strip()
            dns = (settings.VMM.get("DNS") or "").strip()
            prefix_len = settings.VMM.get("NETMASK_PREFIX") or 0
            if not gateway or not dns or not prefix_len:
                auto_gw, auto_dns, auto_prefix = network.detect_gateway(connection)
                gateway = gateway or auto_gw
                dns = dns or auto_dns
                prefix_len = prefix_len or auto_prefix
                if auto_gw:
                    self.stdout.write(
                        f"已从 VMware 网络配置推断：网关 {auto_gw} / DNS {auto_dns} / "
                        f"{auto_prefix} 位前缀（连接 {connection}）"
                    )

            template = VmTemplate.objects.create(
                name=name,
                vmx_path=vmx_path,
                snapshot=(settings.VMM.get("SNAPSHOT") or "").strip(),
                os_family=OsFamily.LINUX,
                network_mode=(NetworkMode.NAT if connection == "nat" else NetworkMode.HOSTONLY),
                guest_user=settings.VMM.get("GUEST_USER") or "root",
                guest_password=settings.VMM.get("GUEST_PASSWORD") or "",
                guest_connection=settings.VMM.get("GUEST_CONNECTION") or "eth0",
                prefix_len=prefix_len or 24,
                gateway=gateway,
                dns=dns,
                default_cpus=info.get("cpus") or settings.VMM["DEFAULT_CPUS"],
                default_memory_mb=info.get("memory_mb") or settings.VMM["DEFAULT_MEMORY_MB"],
                notes="由 bootstrap 从 config.ini 自动创建",
            )
        self.stdout.write(self.style.SUCCESS(f"模板已创建：{template.name}"))
