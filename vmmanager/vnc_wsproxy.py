"""noVNC 的 WebSocket→TCP 桥接代理（纯标准库实现）。

为什么不用 websockify：当前 venv 的 Python 3.14 下，websockify 0.13 的
握手线程会静默退出（http.server 内部行为变化），连 -v 日志都不打——
与其追上游兼容性，不如自写一个最小的 RFC6455 实现，行为完全可控。

职责链：浏览器 noVNC --ws--> 本模块(:6080) --tcp--> 各 VM 的 VMware VNC
token 路由：Django 签发 token 文件（data/vnc_tokens/<token>.tok，
内容 "token: host:port"），浏览器连 ws://host:6080/<token>。
token 是 32 位随机 hex、短时效，且文件由本模块在转发结束后顺带清理
（一次性使用，防止重放）。

只实现 noVNC 需要的最小 RFC6455 子集：二进制帧、ping/pong、close。
noVNC 用 binary 子协议，RFB 消息整段整段地走，不需要分片重组之外的复杂度。
"""

import base64
import hashlib
import logging
import socket
import struct
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def token_dir() -> Path:
    from django.conf import settings

    return Path(settings.DATA_DIR) / "vnc_tokens"


def lookup_token(token: str) -> tuple[str, int] | None:
    """按 token 查转发目标；命中即删文件（一次性），防止 token 重放。"""
    path = token_dir() / f"{token}.tok"
    try:
        text = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    # 行格式 "token: host:port" —— 借用签发时的格式
    try:
        _, target = text.split(":", 1)
        host, port = target.strip().rsplit(":", 1)
        return host, int(port)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# RFC6455 帧编解码（够用就好）
# ---------------------------------------------------------------------------
def read_frame(sock) -> tuple[int, bytes] | None:
    """读一帧，返回 (opcode, payload)；对端关闭返回 None。

    只处理未分片帧——RFB 消息最大也就几 MB，noVNC 不会发出需要
    分片的连续帧；遇到分片直接断开（浏览器端不会这么发）。
    """
    header = _recv_exact(sock, 2)
    if header is None:
        return None
    b1, b2 = header
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        raw = _recv_exact(sock, 2)
        if raw is None:
            return None
        length = struct.unpack("!H", raw)[0]
    elif length == 127:
        raw = _recv_exact(sock, 8)
        if raw is None:
            return None
        length = struct.unpack("!Q", raw)[0]
    if length > 64 * 1024 * 1024:
        raise ValueError("frame too large")

    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if masked and payload:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def encode_frame(payload: bytes) -> bytes:
    """服务端→客户端帧：不掩码。"""
    header = bytes([0x80 | OP_BINARY])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack("!H", n)
    else:
        header += bytes([127]) + struct.pack("!Q", n)
    return header + payload


def _recv_exact(sock, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (ConnectionResetError, OSError):
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _handshake(sock) -> tuple[str, str, str | None]:
    """读 HTTP upgrade 请求，返回 (token, websocket_key, 需回显的子协议)。

    子协议必须**按请求回显**：客户端没声明时绝不能回（RFC 规范，
    Chrome 会直接拒握手——noVNC 1.6 就不带 Sec-WebSocket-Protocol）。
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("client closed before handshake")
        data += chunk
        if len(data) > 65536:
            raise ConnectionError("handshake too large")

    head = data.decode("latin1")
    request_line, *header_lines = head.split("\r\n")
    parts = request_line.split(" ")
    if len(parts) < 2 or parts[0] != "GET":
        raise ConnectionError(f"not a GET request: {request_line[:60]}")
    token = parts[1].strip("/")

    headers = {}
    for line in header_lines:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    if headers.get("upgrade", "").lower() != "websocket":
        raise ConnectionError("missing websocket upgrade header")
    key = headers.get("sec-websocket-key")
    if not key:
        raise ConnectionError("missing sec-websocket-key")

    offered = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    # noVNC 用 binary；客户端声明了就挑一个回，没声明就什么都不回
    protocol = "binary" if "binary" in offered else (offered[0] if offered else None)
    return token, key, protocol


def _accept_response(key: str, protocol: str | None) -> bytes:
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept}",
    ]
    if protocol:
        lines.append(f"Sec-WebSocket-Protocol: {protocol}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _bridge_ws_to_tcp(ws_sock, tcp_sock) -> None:
    """浏览器帧 → 原始 TCP。处理 ping/close，二进制负载原样转发。"""
    try:
        while True:
            frame = read_frame(ws_sock)
            if frame is None:
                logger.info("ws->tcp: client closed")
                break
            opcode, payload = frame
            logger.info("ws->tcp: opcode=%s len=%s", opcode, len(payload))
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                ws_sock.sendall(bytes([0x8A, len(payload)]) + payload)
                continue
            if opcode == OP_PONG or payload == b"" and opcode != OP_BINARY:
                continue
            if opcode != OP_BINARY:
                break
            tcp_sock.sendall(payload)
    except OSError:
        pass
    finally:
        try:
            tcp_sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _bridge_tcp_to_ws(ws_sock, tcp_sock) -> None:
    """原始 TCP → 浏览器帧。"""
    try:
        while True:
            chunk = tcp_sock.recv(65536)
            if not chunk:
                logger.info("tcp->ws: target closed")
                break
            logger.info("tcp->ws: chunk len=%s first=%r", len(chunk), chunk[:16])
            ws_sock.sendall(encode_frame(chunk))
    except OSError:
        pass
    finally:
        try:
            ws_sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle_connection(sock: socket.socket) -> None:
    """单连接处理：握手 → token 校验 → 连目标 → 双向桥接。"""
    sock.settimeout(10)
    try:
        token, key, protocol = _handshake(sock)
    except (ConnectionError, OSError) as exc:
        logger.warning("bad handshake: %r", exc)
        sock.close()
        return

    target = lookup_token(token)
    if target is None:
        logger.warning("invalid or expired VNC token %r", token[:8])
        try:
            sock.sendall(
                b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 25\r\nConnection: close\r\n\r\n"
                b"invalid or expired token\r\n"
            )
        except OSError:
            pass
        sock.close()
        return

    host, port = target
    try:
        tcp_sock = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        logger.warning("cannot reach VNC target %s:%s: %s", host, port, exc)
        try:
            sock.close()
        except OSError:
            pass
        return

    logger.info("bridge open %s:%s (token %s…)", host, port, token[:8])
    bridge_error: list = []

    def _watch(name, fn):
        try:
            fn()
        except Exception:
            # 任何一边意外崩掉都要留全尸——不记下来排查就没线索
            bridge_error.append(name)
            logger.exception("bridge %s crashed", name)

    # down/up 桥各自包一层异常捕获：线程静默死亡是最难排查的故障形态
    try:
        sock.sendall(_accept_response(key, protocol))
        sock.settimeout(None)
        tcp_sock.settimeout(None)
        upstream = threading.Thread(
            target=_watch,
            args=("tcp->ws", lambda: _bridge_tcp_to_ws(sock, tcp_sock)),
            daemon=True,
        )
        upstream.start()
        _watch("ws->tcp", lambda: _bridge_ws_to_tcp(sock, tcp_sock))
        upstream.join(timeout=5)
        if bridge_error:
            logger.info("bridge closed with error (see above)")
    finally:
        logger.info("bridge closed %s:%s (token %s…)", host, port, token[:8])
        for conn in (sock, tcp_sock):
            try:
                conn.close()
            except OSError:
                pass


def proxy_listen() -> tuple[str, int]:
    """监听地址从 settings.VMM 读取。"""
    from django.conf import settings

    return settings.VMM["VNC_PROXY_BIND"], settings.VMM["VNC_PROXY_PORT"]


def serve_forever(bind: str, port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((bind, port))
    server.listen(8)
    logger.info("VNC websocket proxy listening on %s:%s", bind, port)
    token_dir().mkdir(parents=True, exist_ok=True)
    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(
                target=handle_connection,
                args=(conn,),
                daemon=True,
            ).start()
    finally:
        server.close()


def sweep_stale_tokens(ttl: int) -> None:
    """清理过期 token 文件（Django 签发侧也会清，这里是兜底）。"""
    now = time.time()
    for stale in token_dir().glob("*.tok"):
        try:
            if now - stale.stat().st_mtime > ttl:
                stale.unlink(missing_ok=True)
        except OSError:
            pass
