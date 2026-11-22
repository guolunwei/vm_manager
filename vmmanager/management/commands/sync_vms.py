"""命令行同步磁盘（不开网页也能纳管已有虚拟机）。"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from vmmanager.jobs.base import NullContext
from vmmanager.services import inventory


class Command(BaseCommand):
    help = "扫描虚拟机目录，把已有虚拟机纳管进来，并刷新状态与 IP"

    def add_arguments(self, parser):
        parser.add_argument("--no-refresh", action="store_true", help="只同步清单，不刷新状态和 IP")

    def handle(self, *args, **options):
        ctx = NullContext()
        result = inventory.sync_inventory(ctx)
        if not options["no_refresh"]:
            inventory.refresh_all(ctx)
        for line in ctx.lines:
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(
                f"完成：新增 {result['created']}，更新 {result['updated']}，"
                f"跳过模板 {result['skipped']}，文件缺失 {result['missing']}"
            )
        )
