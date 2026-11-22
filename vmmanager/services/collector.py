"""Guest 内指标采集（分钟级时序）。

Workstation 没有宿主侧的单 VM 性能计数器 API（那是 ESXi 的能力），
唯一可行的路径是 **Guest 内采样**：通过 Guest Operations 跑一段脚本，
读 ``/proc/stat``（CPU）和 ``free``（内存），再取回输出。

代价与边界（文档化，避免误期待）：

* 每台机器每轮要 2~3 次 vmrun 往返 + 脚本内 1 秒睡眠，一分钟级采样、
  十台机器约半分钟——够用，但别指望秒级曲线；
* 依赖 open-vm-tools 在跑（与改 IP / 改主机名同一个前提）；
* Windows Guest 不支持（脚本走 shell）。

调度：**链式自续**。每轮采集结束时把下一轮入队（``queued_at = now + interval``），
worker 的 ``claim_next`` 只抢到期任务（``queued_at <= now``），所以未来任务
会安静地排在队列里等待。开关 ``METRICS_ENABLED``，没有单独的调度器进程。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from vmmanager.models import JobKind, JobStatus, OsFamily, VirtualMachine, VmMetric

from . import guest, vmrun

logger = logging.getLogger("vmmanager.collector")

DEDUPE_KEY = "metrics_collect"

# 单次采样脚本：CPU 用 /proc/stat 两次读数的 delta（开机均值没有意义），
# 内存用 free -b。输出一行："cpu% mem_used_mb mem%"
_COLLECT_SCRIPT = r"""
read idle0 total0 <<EOF
$(awk '/^cpu /{print $5, $2+$3+$4+$5+$6+$7+$8}' /proc/stat)
EOF
sleep 1
read idle1 total1 <<EOF
$(awk '/^cpu /{print $5, $2+$3+$4+$5+$6+$7+$8}' /proc/stat)
EOF
awk -v i0="$idle0" -v t0="$total0" -v i1="$idle1" -v t1="$total1" \
    'BEGIN{d=t1-t0; printf "%.1f", (d<=0)?0:(1-(i1-i0)/d)*100}'
free -b | awk '/^Mem:/{printf " %.0f %.1f\n", $3/1048576, $3*100/$2}'
"""


def parse_sample(output: str) -> dict | None:
    """把脚本输出解析成 ``{cpu_percent, mem_used_mb, mem_percent}``。"""
    numbers = []
    for line in (output or "").splitlines():
        parts = line.split()
        for part in parts:
            try:
                numbers.append(float(part))
            except ValueError:
                continue
    if len(numbers) < 3:
        return None
    cpu, mem_used, mem_pct = numbers[0], numbers[1], numbers[2]
    return {
        "cpu_percent": round(min(max(cpu, 0.0), 100.0), 1),
        "mem_used_mb": round(mem_used, 0),
        "mem_percent": round(min(max(mem_pct, 0.0), 100.0), 1),
    }


def collect_one(vm, ctx, *, user: str, password: str) -> VmMetric | None:
    """采一台机器。Guest 不可用就返回 None，不抛异常。"""
    session = guest.GuestSession(vm.vmx_path, user, password)
    try:
        output = session.capture(_COLLECT_SCRIPT, timeout=60)
    except guest.GuestError as exc:
        ctx.warn(f"{vm.name}: guest collection failed: {exc}")
        return None

    sample = parse_sample(output)
    if sample is None:
        ctx.warn(f"{vm.name}: unexpected guest output: {(output or '').strip()[:80]!r}")
        return None

    return VmMetric.objects.create(vm=vm, **sample)


def run_collection(ctx) -> dict:
    """采集全部「该采」的机器。

    「该采」= 磁盘上真实在跑（以 vmrun 为准，不信 DB 缓存的状态）、
    且挂了 Linux 模板的机器。没有模板的机器没有凭据，采不了。
    """
    try:
        running_vmx = vmrun.list_running_set()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to list running VMs: {exc}") from exc

    collected = 0
    skipped = 0

    for vm in VirtualMachine.objects.select_related("template").all():
        if not vm.exists or vmrun.normpath(vm.vmx_path) not in running_vmx:
            continue
        template = vm.template
        if template is None or template.os_family != OsFamily.LINUX:
            skipped += 1
            continue
        ctx.check_cancel()
        password = ctx.get("guest_password") or template.guest_password
        if not password:
            skipped += 1
            continue
        if collect_one(vm, ctx, user=template.guest_user, password=password) is not None:
            collected += 1

    ctx.ok(f"Collected {collected} VMs ({skipped} skipped)")

    retention = int(settings.VMM.get("METRICS_RETENTION_DAYS") or 7)
    cutoff = timezone.now() - timedelta(days=retention)
    purged, _ = VmMetric.objects.filter(created_at__lt=cutoff).delete()
    if purged:
        ctx.info(f"Purged {purged} samples older than {retention} days")

    # 链式自续：下一轮提前入队，靠 queued_at 的未来时间戳等待
    interval = max(60, int(settings.VMM.get("METRICS_INTERVAL") or 300))
    if settings.VMM.get("METRICS_ENABLED"):
        from vmmanager.jobs import queue

        queue.enqueue(
            JobKind.METRICS,
            payload={},
            dedupe_key=DEDUPE_KEY,
            label="指标采集（定时）",
        )
        # 单独把 queued_at 推到未来：enqueue 默认是 now，claim_next 只抢已到期的
        from vmmanager.models import Job

        Job.objects.filter(kind=JobKind.METRICS, status=JobStatus.QUEUED, dedupe_key=DEDUPE_KEY).update(
            queued_at=timezone.now() + timedelta(seconds=interval)
        )
        ctx.info(f"Next collection scheduled in {interval}s")

    return {"collected": collected, "skipped": skipped}


def maybe_enqueue_metrics() -> bool:
    """worker 主循环里调用：到点了且没有在跑的采集任务，就入队一轮。

    「到点」= 最近一次采样距今已超过 METRICS_INTERVAL。全库没有采样时
    （首次启用）立即入队。
    """
    if not settings.VMM.get("METRICS_ENABLED"):
        return False

    from vmmanager.models import Job

    if (
        Job.objects.filter(kind=JobKind.METRICS, status=JobStatus.QUEUED, dedupe_key=DEDUPE_KEY).exists()
        or Job.objects.filter(kind=JobKind.METRICS, status=JobStatus.RUNNING).exists()
    ):
        return False

    latest = VmMetric.objects.aggregate(latest=Max("created_at"))["latest"]
    if latest is not None:
        interval = max(60, int(settings.VMM.get("METRICS_INTERVAL") or 300))
        if (timezone.now() - latest).total_seconds() < interval:
            return False

    from vmmanager.jobs import queue

    queue.enqueue(JobKind.METRICS, payload={}, dedupe_key=DEDUPE_KEY, label="指标采集")
    return True


# ---------------------------------------------------------------------------
# 查询与图表
# ---------------------------------------------------------------------------
def metric_series(vm: VirtualMachine, *, hours: int = 24, max_points: int = 120) -> list[dict]:
    """取一台机器的时序样本，按时间升序，超过 ``max_points`` 就等距抽样。

    只做降采样不做插值——曲线是真实采样点的连线，不造假数据。
    """
    cutoff = timezone.now() - timedelta(hours=hours)
    rows = list(
        VmMetric.objects.filter(vm=vm, created_at__gte=cutoff)
        .order_by("created_at")
        .values("created_at", "cpu_percent", "mem_percent", "mem_used_mb")
    )
    if len(rows) > max_points:
        step = -(-len(rows) // max_points)  # ceil div
        rows = rows[::step]
    return [
        {
            "time": row["created_at"].strftime("%m-%d %H:%M"),
            "cpu_percent": row["cpu_percent"],
            "mem_percent": row["mem_percent"],
            "mem_used_mb": row["mem_used_mb"],
        }
        for row in rows
    ]


def chart_data(vm: VirtualMachine, *, hours: int = 24, width: int = 560, height: int = 90) -> dict:
    """把时序样本换成 SVG 折线的坐标串（viewBox 0..width / 0..height）。

    CPU 和内存都按 0~100% 归一，两条线可以共用一个坐标系，一眼对比。
    """
    series = metric_series(vm, hours=hours)
    empty = {"series": [], "cpu_points": "", "mem_points": "", "hours": hours}
    if len(series) < 2:
        return empty

    def points(key: str) -> str:
        coords = []
        n = len(series)
        for i, row in enumerate(series):
            x = round(i / (n - 1) * width, 1)
            value = row[key] or 0
            y = round(height - min(max(value, 0.0), 100.0) / 100 * height, 1)
            coords.append(f"{x},{y}")
        return " ".join(coords)

    return {
        "series": series,
        "cpu_points": points("cpu_percent"),
        "mem_points": points("mem_percent"),
        "hours": hours,
        "latest": series[-1],
    }
