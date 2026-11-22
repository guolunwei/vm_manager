"""``.vmx`` 读写的回归测试。

这里的每个用例都对应一次真实的线上故障，删改前请先看 ``vmx.py`` 的模块文档。
"""

from __future__ import annotations

import pytest

from vmmanager.services import vmx

#: 复刻真实模板的特征：GBK 编码声明、CRLF 换行、键名大小写不一致（memsize）
TEMPLATE = """\
.encoding = "GBK"
displayName = "CentOS7_cloud"
guestOS = "centos7-64"
numvcpus = "2"
cpuid.coresPerSocket = "2"
memsize = "4096"
ethernet0.present = "TRUE"
ethernet0.addressType = "generated"
ethernet0.generatedAddress = "00:0c:29:f6:35:ce"
annotation = "这是一个中文注释"
"""


@pytest.fixture
def vmx_file(tmp_path):
    """写一个 GBK + CRLF 的 vmx，模拟 VMware 在中文 Windows 下的产物。"""
    path = tmp_path / "test.vmx"
    path.write_bytes(TEMPLATE.replace("\n", "\r\n").encode("gbk"))
    return path


def _keys(path) -> list[str]:
    text = path.read_bytes().decode("gbk")
    return [
        line.split("=")[0].strip().lower()
        for line in text.splitlines()
        if "=" in line and not line.strip().startswith(("#", "//"))
    ]


def test_patch_matches_key_case_insensitively(vmx_file):
    """传 ``memSize`` 必须改写文件里已有的 ``memsize``，不能追加成第二个键。

    这是最严重的一次故障：追加出重复键后 VMware 直接报
    ``Cannot read the virtual machine configuration file`` 拒绝开机。
    """
    changed = vmx.patch(str(vmx_file), {"memSize": 2048})

    assert changed == ["memsize"], "应当报告改写了文件里原有的键名"
    data = vmx.read(str(vmx_file))
    assert data["memSize"] == "2048"
    assert data["memsize"] == "2048"

    keys = _keys(vmx_file)
    assert keys.count("memsize") == 1, f"出现了重复键：{keys}"
    assert "memsize" in _raw_text(vmx_file), "应沿用文件里原来的小写写法"


def _raw_text(path) -> str:
    return path.read_bytes().decode("gbk")


def test_patch_preserves_crlf(vmx_file):
    """CRLF 不能被换成 LF，否则 VMware 同样拒绝读配置。"""
    vmx.patch(str(vmx_file), {"displayName": "vm-1"})

    raw = vmx_file.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "出现裸 LF，换行被转换了"
    assert b"\r\n" in raw


def test_patch_preserves_declared_encoding(vmx_file):
    """按 ``.encoding`` 声明读写，中文注释不能变成乱码或问号。"""
    vmx.patch(str(vmx_file), {"numvcpus": 4})

    raw = vmx_file.read_bytes()
    assert b'.encoding = "GBK"' in raw
    assert "这是一个中文注释".encode("gbk") in raw


def test_patch_appends_missing_key(vmx_file):
    """文件里没有的键才追加到末尾。"""
    changed = vmx.patch(str(vmx_file), {"vcpu.hotadd": "TRUE"})

    assert changed == ["vcpu.hotadd"]
    assert vmx.read(str(vmx_file))["vcpu.hotadd"] == "TRUE"


def test_patch_is_noop_when_value_unchanged(vmx_file):
    before = vmx_file.read_bytes()
    assert vmx.patch(str(vmx_file), {"displayName": "CentOS7_cloud"}) == []
    assert vmx_file.read_bytes() == before


def test_patch_skips_none_values(vmx_file):
    """``None`` 表示「不改」，不能被写成字符串 ``"None"``。"""
    assert vmx.patch(str(vmx_file), {"displayName": None}) == []
    assert vmx.read(str(vmx_file))["displayName"] == "CentOS7_cloud"


def test_read_is_case_insensitive(vmx_file):
    data = vmx.read(str(vmx_file))
    assert data["memSize"] == "4096"
    assert data["memsize"] == "4096"
    assert data.get("MEMORY", "fallback") == "fallback"


def test_memory_mb_reads_lowercase_key(vmx_file):
    """``memory_mb()`` 曾经因为只认 ``memSize`` 而永远返回默认值。"""
    assert vmx.memory_mb(str(vmx_file)) == 4096


def test_patch_keeps_comments(vmx_file):
    vmx.patch(str(vmx_file), {"numvcpus": 8})
    assert "这是一个中文注释" in _raw_text(vmx_file)


@pytest.mark.parametrize("numvcpus,expected", [(1, 1), (2, 2), (3, 3), (4, 4), (18, 18)])
def test_cpu_topology(numvcpus, expected):
    """单路拓扑：coresPerSocket 恒等于 vCPU 总数，路数恒为 1。"""
    cores = vmx.cpu_topology(numvcpus)
    assert cores == expected
    assert numvcpus % cores == 0
    assert numvcpus // cores == 1, "必须只有一路，避免客户机按路数受限"


def test_cpu_topology_never_zero():
    for value in range(1, 33):
        assert vmx.cpu_topology(value) >= 1
