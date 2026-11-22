"""一条命令同时启动 Web 与 worker。

背景：任务队列需要一个 worker 进程消费；以前要先 ``start.bat``（两个窗口）
或分别敲 ``runserver`` 和 ``worker``，忘了 worker 页面就永远「排队中」。
现在 ``python manage.py serve`` 一条命令把两个进程一起拉起来，Ctrl+C 一起退。

用法::

    python manage.py serve                    # 127.0.0.1:8000 + worker + VNC 代理
    python manage.py serve 0.0.0.0:8000       # 指定监听地址
    python manage.py serve --noreload         # 关掉自动重载（给 debugger 用）
    python manage.py serve --no-worker        # 只起 web（worker 另行管理）
    python manage.py serve --no-vncproxy      # 不启动 noVNC 的 websockify 代理

实现说明：worker 是子进程（不是线程），与 runserver 的进程边界一致——
autoreload 重启 web 不会把 worker 一起杀掉重来；Windows 下用 taskkill 也能
单独清理。worker 的 stdout/stderr 直接透传，日志和 web 混排但都带前缀。
"""

from __future__ import annotations

import os
import subprocess
import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "同时启动 Web 服务与后台 worker（单机部署 / 开发用）"

    def add_arguments(self, parser):
        parser.add_argument(
            "addrport",
            nargs="?",
            default="127.0.0.1:8000",
            help="Web 监听地址，默认 127.0.0.1:8000",
        )
        parser.add_argument(
            "--noreload",
            action="store_true",
            help="关闭 runserver 自动重载（调试器下建议加上）",
        )
        parser.add_argument(
            "--no-worker",
            action="store_true",
            help="只启动 Web，不拉起 worker",
        )
        parser.add_argument(
            "--no-vncproxy",
            action="store_true",
            help="不启动 noVNC 的 websockify 代理",
        )

    def handle(self, *args, **options):
        addrport: str = options["addrport"]
        noreload: bool = options["noreload"]
        want_worker: bool = not options["no_worker"]
        want_vncproxy: bool = not options["no_vncproxy"]

        # 防重复拉起的标记：Django 各版本重载机制差异很大（RUN_MAIN 的取值
        # 和子进程跑的命令都变过），与其耦合不如自己设环境变量——父进程执行
        # handle 前先打标，任何重新执行的子进程（无论跑 serve 还是 runserver）
        # 都会继承这个标记，从而不再拉起第二份 worker/vncproxy。
        marked = os.environ.get("VMM_SERVE_CHILDREN") == "1"
        want_children = (want_worker or want_vncproxy) and not marked
        if want_children:
            os.environ["VMM_SERVE_CHILDREN"] = "1"

        worker_proc: subprocess.Popen | None = None
        vnc_proc: subprocess.Popen | None = None
        if want_children:
            if want_worker:
                worker_proc = self._spawn("worker")
                self.stdout.write(self.style.SUCCESS(f"[serve] worker 已启动 (pid {worker_proc.pid})"))
            if want_vncproxy:
                vnc_proc = self._spawn("vncproxy")
                self.stdout.write(self.style.SUCCESS(f"[serve] vncproxy 已启动 (pid {vnc_proc.pid})"))
        elif want_worker or want_vncproxy:
            self.stdout.write("[serve] 子进程已由父进程拉起，本进程不重复启动")

        try:
            argv = ["manage.py", "runserver", addrport]
            if noreload:
                argv.append("--noreload")
            # 走 execute_from_command_line 而不是 call_command：
            # runserver 的 reloader 依赖 sys.argv，call_command 会绕坏。
            from django.core.management import execute_from_command_line

            sys.argv = argv
            execute_from_command_line(argv)
        finally:
            for proc, name in ((worker_proc, "worker"), (vnc_proc, "vncproxy")):
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    self.stdout.write(f"[serve] {name} 已停止")

    def _spawn(self, command: str) -> subprocess.Popen:
        """以当前解释器和设置模块启动独立子进程（worker / vncproxy）。"""
        env = os.environ.copy()
        env.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
        # PYTHONUNBUFFERED 让 worker 的 print/log 实时透传到本窗口
        env.setdefault("PYTHONUNBUFFERED", "1")
        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP：terminate 时能整体带走子进程树
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            [sys.executable, str(os.path.join(os.getcwd(), "manage.py")), command],
            env=env,
            creationflags=creationflags,
        )
