"""noVNC 的 WebSocket 代理：桥接浏览器 noVNC 到各 VM 的 VMware VNC。

浏览器 noVNC --ws--> 本命令(:6080) --tcp--> 各虚拟机的 VMware VNC(:59xx)
路由靠 token：Django 签发 token 文件到 data/vnc_tokens/，浏览器连
ws://host:6080/<token>，代理按 token 找目标并顺带销毁 token（一次性）。
token 短时效、不可预测，别人拿不到就进不来。

单独运行：python manage.py vncproxy
一般不需要手动跑——`manage.py serve` 会自动拉起。
（曾经用 websockify 做桥接，在 Python 3.14 下握手线程静默失败，
遂改为自带的纯标准库实现，见 vmmanager/vnc_wsproxy.py。）
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from vmmanager.vnc_wsproxy import proxy_listen, serve_forever, sweep_stale_tokens

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "启动 noVNC 的 WebSocket 代理（token 路由 -> 各 VM 的 VNC 端口）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="只打印监听地址，不真正启动（调试用）",
        )

    def handle(self, *args, **options):
        bind, port = proxy_listen()
        if options["check"]:
            self.stdout.write(f"vncproxy would listen on {bind}:{port}")
            return

        self.stdout.write(f"VNC proxy on {bind}:{port}")
        sweep_stale_tokens(settings.VMM["VNC_TOKEN_TTL"])
        logger.info("starting VNC websocket proxy on %s:%s", bind, port)
        serve_forever(bind, port)
