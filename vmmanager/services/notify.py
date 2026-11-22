"""任务终态通知：企业微信 / 钉钉 webhook 与邮件。

设计要点：

* **worker 侧推送**。任务终态由 worker 写库，通知也在 worker 里发——Web 进程
  不该为了发通知被拖住（webhook 慢、SMTP 更慢）。
* **渠道独立、永不抛出**。某个 webhook 配错了不能影响另一个渠道，更不能让
  「任务已完成」这件事本身报错。调用方（worker）拿不到任何异常。
* **默认只在失败时通知**。成功的通知很快就会变成噪音；需要时打开
  ``NOTIFY_ON_SUCCESS``。
* 邮件必须配置了 SMTP（``EMAIL_HOST``）才发，否则直接跳过——没有 SMTP
  还硬发只会刷出异常日志。

配置（环境变量 / .env / config.ini 均可）::

    NOTIFY_WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    NOTIFY_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
    NOTIFY_EMAIL_TO=ops@example.com,boss@example.com
    NOTIFY_ON_FAILED=1        # 默认开
    NOTIFY_ON_SUCCESS=0       # 默认关
    NOTIFY_ON_CANCELED=0      # 默认关
"""

from __future__ import annotations

import json as _json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail import send_mail

from vmmanager.models import JobStatus

logger = logging.getLogger("vmmanager.notify")

_TIMEOUT = 10  # webhook 超时（秒）。通知不该比任务本身还慢


def _channel_configs() -> list[str]:
    """已配置的渠道名列表，全空则调用方直接跳过。"""
    vmm = settings.VMM
    names = []
    if vmm.get("NOTIFY_WECHAT_WEBHOOK"):
        names.append("wechat")
    if vmm.get("NOTIFY_DINGTALK_WEBHOOK"):
        names.append("dingtalk")
    if vmm.get("NOTIFY_EMAIL_TO"):
        names.append("email")
    return names


def _status_wanted(status: str) -> bool:
    vmm = settings.VMM
    if status == JobStatus.FAILED:
        return bool(vmm.get("NOTIFY_ON_FAILED", True))
    if status == JobStatus.SUCCEEDED:
        return bool(vmm.get("NOTIFY_ON_SUCCESS", False))
    if status == JobStatus.CANCELED:
        return bool(vmm.get("NOTIFY_ON_CANCELED", False))
    return False


def _message(job) -> str:
    """一条通知的正文。企微/钉钉直接用纯文本，邮件单独拼主题。"""
    vm_name = ""
    if job.vm_id:
        vm_name = job.vm.name if job.vm else (job.label or "")
    target = vm_name or job.label or f"#{job.pk}"
    lines = [
        f"[{job.get_status_display()}] {job.get_kind_display()} — {target}",
        f"任务 #{job.pk} · 耗时 {job.duration}s · 发起人 {job.created_by.username if job.created_by else '—'}",
    ]
    if job.status == JobStatus.FAILED and job.error:
        first_line = str(job.error).strip().splitlines()[0][:300]
        lines.append(f"原因：{first_line}")
    return "\n".join(lines)


def _post_json(url: str, payload: dict) -> None:
    """极简 POST。用标准库，不为一条通知引入 requests。"""
    request = urllib.request.Request(
        url,
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        response.read()


def _send_wechat(text: str) -> None:
    url = settings.VMM["NOTIFY_WECHAT_WEBHOOK"]
    _post_json(url, {"msgtype": "text", "text": {"content": text}})


def _send_dingtalk(text: str) -> None:
    url = settings.VMM["NOTIFY_DINGTALK_WEBHOOK"]
    _post_json(url, {"msgtype": "text", "text": {"content": text}})


def _send_email(job, text: str) -> None:
    to = [item.strip() for item in settings.VMM["NOTIFY_EMAIL_TO"].split(",") if item.strip()]
    if not to:
        return
    vm_name = (job.vm.name if job.vm else "") or job.label or f"#{job.pk}"
    subject = f"[{job.get_status_display()}] {job.get_kind_display()} — {vm_name}"
    send_mail(
        subject=subject,
        message=text,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "vmmanager@localhost"),
        recipient_list=to,
        fail_silently=False,
    )


def notify_job_finished(job) -> list[str]:
    """按配置推送终态通知，返回实际发送成功的渠道名。

    任何渠道的失败都只记日志，不抛异常——通知是锦上添花，不是任务的一部分。
    """
    if not _status_wanted(job.status):
        return []
    if not _channel_configs():
        return []

    text = _message(job)
    sent: list[str] = []

    for name in _channel_configs():
        try:
            if name == "wechat":
                _send_wechat(text)
            elif name == "dingtalk":
                _send_dingtalk(text)
            elif name == "email":
                if not getattr(settings, "EMAIL_HOST", ""):
                    logger.info("Email notify skipped: EMAIL_HOST is not configured")
                    continue
                _send_email(job, text)
            sent.append(name)
        except Exception as exc:  # noqa: BLE001 — 单渠道失败不影响其余渠道
            logger.warning("Notify via %s failed for job #%s: %s", name, job.pk, exc)

    if sent:
        logger.info("Job #%s notification sent via %s", job.pk, ",".join(sent))
    return sent
