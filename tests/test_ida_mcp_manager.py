"""IDA-MCP 实例管理器（core/tools/ida_mcp_manager.py）hermetic 测试。

fake popen/probe 全注入，不触网、不拉真 IDA；样本实例桥的选路行为一并覆盖。
"""

from __future__ import annotations

import time

import pytest

import core.tools.ida_mcp_manager as mm
from core.tools.decompiler import DecompilerService
from core.tools.ida_mcp_manager import IdaMcpManager


class FakeProc:
    def __init__(self, args):
        self.args = args
        self.pid = 4242
        self.killed = False

    def kill(self):
        self.killed = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    """无 IDA 环境（resolve 恒 None）+ fake 拉起/查杀件。"""
    monkeypatch.setattr(mm, "resolve_ida_headless", lambda: None)
    killed: list = []
    monkeypatch.setattr(mm, "_kill_tree", lambda proc: killed.append(proc))
    bootstrap = tmp_path / "bootstrap_mcp.py"
    bootstrap.write_text("# fake bootstrap", encoding="utf-8")
    fake_idat = tmp_path / "idat.exe"
    fake_idat.write_bytes(b"MZ")
    db_dir = tmp_path / "db"
    launched: list[FakeProc] = []

    def make(*, probe=True, **kw):
        def popen(args, env=None):
            proc = FakeProc(args)
            proc.env = env
            launched.append(proc)
            return proc

        kw.setdefault("probe_interval", 0.01)
        kw.setdefault("reaper_interval", 0.05)
        return IdaMcpManager(bootstrap=bootstrap, idat_cmd=str(fake_idat),
                             db_dir=db_dir, popen=popen,
                             probe=lambda ep: probe, **kw)

    return make, launched, killed, tmp_path, db_dir


def _binary(tmp_path):
    b = tmp_path / "sample.exe"
    b.write_bytes(b"\x4d\x5a" + b"\x00" * 16)
    return str(b)


def test_ensure_launches_and_reuses_instance(env):
    make, launched, killed, tmp_path, db_dir = env
    m = make(ready_timeout=1.0)
    binary = _binary(tmp_path)
    ep = m.ensure("p1", binary)
    assert ep == "http://127.0.0.1:13338/mcp"
    assert len(launched) == 1
    # 命令形态：-A -S<bootstrap 无空格单参数>（端口走环境变量，list2cmdline 引号坑）
    # -o<db_stem> binary；bootstrap 端口与端点一致
    args = launched[0].args
    assert "-A" in args and f"-o{db_dir / mm.sha256_file(binary)}" in args
    assert binary == args[-1]
    assert any(a.startswith("-S") and " " not in a for a in args)
    assert launched[0].env["CYBERSTRIKE_IDA_MCP_PORT"] == "13338"
    # 复用：同项目同样本第二次 ensure 不再拉起
    assert m.ensure("p1", binary) == ep
    assert len(launched) == 1


def test_ensure_reaper_and_shutdown_kill_processes(env):
    make, launched, killed, tmp_path, _ = env
    m = make(ready_timeout=1.0, idle_timeout=0.05)
    binary = _binary(tmp_path)
    assert m.ensure("p1", binary)
    assert m.online_for_project("p1")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and m.online_for_project("p1"):
        time.sleep(0.05)
    assert not m.online_for_project("p1")     # 空闲超时被 reaper 收走
    assert launched[0] in killed
    # shutdown 兜关：清空 + 拒绝新拉起
    assert m.ensure("p1", binary)
    m.shutdown_all()
    assert launched[-1] in killed
    assert m.ensure("p1", binary) is None     # stopped 后恒 None


def test_ensure_returns_none_without_ida(env):
    """无 IDA（resolve None 且未注入 idat_cmd）→ ensure 恒 None，零拉起。"""
    make, launched, killed, tmp_path, _ = env
    bootstrap = tmp_path / "bootstrap_mcp.py"
    m = IdaMcpManager(bootstrap=bootstrap, db_dir=tmp_path / "db",
                      popen=lambda args: pytest.fail("不应拉起"),
                      probe=lambda ep: True)
    assert m.ensure("p1", _binary(tmp_path)) is None
    assert launched == []


def test_ensure_ready_timeout_kills_instance(env):
    """就绪超时：返回 None 且实例被杀（留半死实例会和 headless 导出抢 db）。"""
    make, launched, killed, tmp_path, _ = env
    m = make(probe=False, ready_timeout=0.03)
    assert m.ensure("p1", _binary(tmp_path)) is None
    assert launched[0] in killed
    assert m.online_for_project("p1") is False


def test_instance_limit_lru_eviction(env):
    make, launched, killed, tmp_path, _ = env
    m = make(ready_timeout=1.0, max_instances=1)
    b1 = tmp_path / "a.exe"
    b1.write_bytes(b"\x4d\x5a\x01")
    b2 = tmp_path / "b.exe"
    b2.write_bytes(b"\x4d\x5a\x02")
    assert m.ensure("p1", str(b1))
    assert m.ensure("p1", str(b2))
    assert len(launched) == 2
    assert launched[0] in killed              # LRU 关最旧
    assert m.online_for_project("p1")


def test_db_lock_skips_launch(env):
    """GUI 开着该库（锁文件在）→ 不拉起、返回 None（同 headless 写回锁语义）。"""
    make, launched, killed, tmp_path, db_dir = env
    db_dir.mkdir(parents=True)
    sha = mm.sha256_file(_binary(tmp_path))
    (db_dir / sha).with_suffix(".id0").write_bytes(b"")
    m = make(ready_timeout=1.0)
    assert m.ensure("p1", _binary(tmp_path)) is None
    assert launched == []


def test_no_instance_launch_on_lock_free_gui_absent(env):
    make, launched, killed, tmp_path, db_dir = env
    m = make(ready_timeout=1.0)
    assert m.ensure("p1", _binary(tmp_path))
    # online_for_project 只认本项目的实例
    assert m.online_for_project("other") is False


# ---------- DecompilerService 样本实例桥（mcp_provider 接线） ----------

def test_sample_mcp_provider_routing(tmp_path):
    binary = _binary(tmp_path)
    svc = DecompilerService(cache_dir=tmp_path / "cache",
                            mcp_provider=lambda b: "http://127.0.0.1:13400/mcp")
    bridge = svc._sample_mcp(binary)
    assert bridge is not None and bridge.endpoint == "http://127.0.0.1:13400/mcp"
    # 桥缓存复用（同端点不再新建）
    assert svc._sample_mcp(binary) is bridge
    # 与固定桥同端点 → 直接复用固定桥，不另建
    fixed = DecompilerService(cache_dir=tmp_path / "cache",
                              mcp_endpoint="http://127.0.0.1:13337/mcp",
                              mcp_provider=lambda b: "http://127.0.0.1:13337/mcp")
    assert fixed._sample_mcp(binary) is fixed.mcp


def test_sample_mcp_provider_failure_degrades(tmp_path):
    """provider 失败/返回 None：退回固定桥或无桥，行为与现状一致（不抛）。"""
    binary = _binary(tmp_path)

    def bad_provider(_b):
        raise RuntimeError("boom")

    svc = DecompilerService(cache_dir=tmp_path / "cache", mcp_provider=bad_provider)
    assert svc._sample_mcp(binary) is None
    assert svc.decompile(binary, address=0x1189).startswith("[反编译器不可用]")
    svc2 = DecompilerService(cache_dir=tmp_path / "cache",
                             mcp_provider=lambda b: None)
    assert svc2._sample_mcp(binary) is None
