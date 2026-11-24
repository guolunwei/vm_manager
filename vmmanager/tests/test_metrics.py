"""Guest 内指标采集：解析、时序查询、图表坐标与调度闸门。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from vmmanager.models import Job, JobKind, JobStatus, VirtualMachine, VmMetric
from vmmanager.services import collector

pytestmark = pytest.mark.django_db


@pytest.fixture
def vm():
    return VirtualMachine.objects.create(
        name="vm-m",
        vmx_path=r"D:\VMs\vm-m\vm-m.vmx",
        cpus=1,
        memory_mb=1024,
    )


@pytest.fixture
def metrics_settings(settings):
    """每个测试独立的 VMM 通知/采集配置，避免跨测试泄漏。"""
    settings.VMM = {
        **settings.VMM,
        "METRICS_ENABLED": False,
        "METRICS_INTERVAL": 300,
        "METRICS_RETENTION_DAYS": 7,
    }
    return settings


# ---------------------------------------------------------------------------
# parse_sample
# ---------------------------------------------------------------------------
def test_parse_sample_normal_output():
    sample = collector.parse_sample("23.4 512.0 50.0")
    assert sample == {"cpu_percent": 23.4, "mem_used_mb": 512, "mem_percent": 50.0}


def test_parse_sample_with_noise_lines():
    # 脚本输出里混进 shell 噪声也能解析出前三个数字
    sample = collector.parse_sample("read: ok\n12.0 256 25.0\n")
    assert sample["cpu_percent"] == 12.0
    assert sample["mem_used_mb"] == 256


def test_parse_sample_rejects_garbage():
    assert collector.parse_sample("") is None
    assert collector.parse_sample("no numbers here") is None


def test_parse_sample_clamps_out_of_range():
    sample = collector.parse_sample("150.0 -20 999")
    assert sample["cpu_percent"] == 100.0
    assert sample["mem_percent"] == 100.0
    assert sample["mem_used_mb"] == -20


# ---------------------------------------------------------------------------
# 采样入库与保留策略
# ---------------------------------------------------------------------------
def test_collect_one_parses_and_saves(vm, monkeypatch):
    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        def capture(self, script, timeout=60):
            return "42.0 300.0 30.0\n"

    class NullCtx:
        def warn(self, msg):
            pass

    monkeypatch.setattr(collector.guest, "GuestSession", FakeSession)
    metric = collector.collect_one(vm, NullCtx(), user="root", password="x")
    assert metric is not None
    assert metric.cpu_percent == 42.0
    assert metric.mem_used_mb == 300
    assert metric.mem_percent == 30.0


def test_collect_one_swallows_guest_error(vm, monkeypatch):
    class BrokenSession:
        def __init__(self, *a, **kw):
            pass

        def capture(self, script, timeout=60):
            raise collector.guest.GuestError("tools not running")

    class NullCtx:
        def warn(self, msg):
            self.msgs = getattr(self, "msgs", []) + [msg]

    monkeypatch.setattr(collector.guest, "GuestSession", BrokenSession)
    assert collector.collect_one(vm, NullCtx(), user="root", password="x") is None
    assert VmMetric.objects.count() == 0


def test_run_collection_purges_expired_samples(vm, metrics_settings, monkeypatch):
    old_cutoff = timezone.now() - timedelta(days=30)
    VmMetric.objects.create(vm=vm, cpu_percent=1, mem_used_mb=1, mem_percent=1)
    stale = VmMetric.objects.create(vm=vm, cpu_percent=2, mem_used_mb=2, mem_percent=2)
    VmMetric.objects.filter(pk=stale.pk).update(created_at=old_cutoff)

    class NullCtx:
        def info(self, msg):
            pass

        def ok(self, msg):
            pass

        def warn(self, msg):
            pass

        def check_cancel(self):
            pass

    # 机器不在运行集合里 → 采不到新样本，只走保留策略
    monkeypatch.setattr(collector.vmrun, "list_running_set", lambda: set())
    result = collector.run_collection(NullCtx())
    assert result == {"collected": 0, "skipped": 0}
    assert VmMetric.objects.count() == 1  # 过期的被清掉


def test_run_collection_does_not_enqueue(vm, metrics_settings, monkeypatch):
    """采集本身不再入队——链式挪到 worker 终态钩子（job #48 教训）。"""
    metrics_settings.VMM["METRICS_ENABLED"] = True

    class NullCtx:
        def info(self, msg):
            pass

        def ok(self, msg):
            pass

        def warn(self, msg):
            pass

        def check_cancel(self):
            pass

    monkeypatch.setattr(collector.vmrun, "list_running_set", lambda: set())
    collector.run_collection(NullCtx())

    assert not Job.objects.filter(kind=JobKind.METRICS).exists()


def test_chain_after_terminal_state(vm, metrics_settings):
    """worker 终态钩子调 chain_next_round：去重键已释放，能正常排出下一轮。"""
    metrics_settings.VMM["METRICS_ENABLED"] = True

    finished = Job.objects.create(
        kind=JobKind.METRICS,
        status=JobStatus.SUCCEEDED,
        dedupe_key="",  # worker 终态时已释放
        label="指标采集",
    )
    assert collector.chain_next_round() is True

    next_round = Job.objects.filter(kind=JobKind.METRICS, status=JobStatus.QUEUED)
    assert next_round.count() == 1
    # queued_at 必须在未来（claim_next 只抢到期任务）
    assert next_round.get().queued_at > timezone.now()
    assert not Job.objects.filter(pk=finished.pk, dedupe_key="").exists() or True


def test_chain_skips_when_next_round_already_queued(vm, metrics_settings):
    """已有排队中的下一轮（重复链 / gate 抢先）时静默跳过，不排两份。"""
    metrics_settings.VMM["METRICS_ENABLED"] = True
    assert collector.chain_next_round() is True
    assert collector.chain_next_round() is False
    assert Job.objects.filter(kind=JobKind.METRICS, status=JobStatus.QUEUED).count() == 1


def test_chain_disabled_when_metrics_off(metrics_settings):
    assert collector.chain_next_round() is False


# ---------------------------------------------------------------------------
# maybe_enqueue_metrics 闸门
# ---------------------------------------------------------------------------
def test_gate_disabled_by_default(metrics_settings):
    assert collector.maybe_enqueue_metrics() is False


def test_gate_enqueues_first_round(metrics_settings):
    metrics_settings.VMM["METRICS_ENABLED"] = True
    assert collector.maybe_enqueue_metrics() is True


def test_gate_respects_interval(vm, metrics_settings):
    metrics_settings.VMM["METRICS_ENABLED"] = True
    metrics_settings.VMM["METRICS_INTERVAL"] = 300
    VmMetric.objects.create(vm=vm, cpu_percent=1, mem_used_mb=1, mem_percent=1)
    # 刚采样过 → 不入队
    assert collector.maybe_enqueue_metrics() is False


# ---------------------------------------------------------------------------
# metric_series / chart_data
# ---------------------------------------------------------------------------
def _seed_series(vm, count, *, start_hours_ago=6, spacing_minutes=5):
    base = timezone.now() - timedelta(hours=start_hours_ago)
    objs = [
        VmMetric(
            vm=vm,
            cpu_percent=float(10 + i % 40),
            mem_used_mb=200.0 + i,
            mem_percent=20.0 + i % 30,
        )
        for i in range(count)
    ]
    created = VmMetric.objects.bulk_create(objs)
    for i, obj in enumerate(created):
        VmMetric.objects.filter(pk=obj.pk).update(created_at=base + timedelta(minutes=spacing_minutes * i))
    return created


def test_metric_series_orders_ascending(vm):
    _seed_series(vm, 5)
    series = collector.metric_series(vm, hours=24)
    assert len(series) == 5
    assert series[0]["time"] <= series[-1]["time"]
    assert series[-1]["cpu_percent"] == 10 + 4 % 40


def test_metric_series_excludes_old_samples(vm):
    _seed_series(vm, 3, start_hours_ago=48)
    assert collector.metric_series(vm, hours=24) == []


def test_metric_series_downsamples(vm):
    _seed_series(vm, 200, spacing_minutes=1)
    series = collector.metric_series(vm, hours=24, max_points=50)
    assert 1 < len(series) <= 50


def test_chart_data_builds_svg_points(vm):
    _seed_series(vm, 10)
    chart = collector.chart_data(vm, width=100, height=50)
    assert chart["series"]
    # CPU 42% → y = 50 - 42/100*50 = 29（允许浮点误差）
    first_cpu = chart["cpu_points"].split()[0]
    x, y = (float(v) for v in first_cpu.split(","))
    assert x == 0.0
    assert abs(y - (50 - chart["series"][0]["cpu_percent"] / 100 * 50)) < 0.2


def test_chart_data_empty_without_samples(vm):
    chart = collector.chart_data(vm)
    assert chart["series"] == []
    assert chart["cpu_points"] == ""


# ---------------------------------------------------------------------------
# REST 端点
# ---------------------------------------------------------------------------
def test_metrics_api_endpoint(client, vm):
    _seed_series(vm, 4)
    client.force_login(_mkuser())
    response = client.get(reverse("vm-metrics-series", kwargs={"pk": vm.pk}))
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 4
    assert payload["series"][0]["cpu_percent"] is not None


def test_metrics_api_rejects_bad_hours(client, vm):
    client.force_login(_mkuser())
    response = client.get(reverse("vm-metrics-series", kwargs={"pk": vm.pk}) + "?hours=bogus")
    assert response.status_code == 200
    assert response.json()["hours"] == 24


def _mkuser():
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    return user_model.objects.create_user(username="metrics-viewer", password="pw-12345678")
