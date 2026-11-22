"""运行后台任务 worker。"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from vmmanager.jobs.worker import run_forever


class Command(BaseCommand):
    help = "启动后台任务 worker（处理克隆、开机、改 IP、快照等耗时操作）"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="队列跑空后退出，用于测试或定时触发")
        parser.add_argument("--name", default="", help="worker 名称，默认 主机名-PID-随机串")
        parser.add_argument("--verbose", action="store_true", help="打印调试日志")

    def handle(self, *args, **options):
        if options["verbose"]:
            logging.getLogger("vmmanager").setLevel(logging.DEBUG)
        self.stdout.write(self.style.MIGRATE_HEADING("worker 启动，Ctrl+C 退出"))
        run_forever(name=options["name"], once=options["once"])
