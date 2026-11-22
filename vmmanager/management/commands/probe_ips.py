"""探测所有运行中虚拟机的 IP（命令行版）。"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from vmmanager.jobs.base import NullContext
from vmmanager.services import inventory


class Command(BaseCommand):
    help = "探测运行中虚拟机的真实 IP 并回填到数据库"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="包括已有 IP 的机器（会重新核对）")
        parser.add_argument("--no-scan", action="store_true", help="不扫描网段，只查 Tools 和 ARP")

    def handle(self, *args, **options):
        ctx = NullContext()
        result = inventory.probe_ips(
            ctx,
            only_missing=not options["all"],
            allow_scan=not options["no_scan"],
        )
        for line in ctx.lines:
            self.stdout.write(line)
        self.stdout.write(self.style.SUCCESS(f"探测完成：{result['found']}/{result['checked']}"))
