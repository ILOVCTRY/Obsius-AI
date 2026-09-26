"""工具链注册表测试（toolchain-registry M1，2026-09-23）。

覆盖：schema 校验（fail-fast）/ 覆盖层容错 / 四来源优先级 / 盘符通配 /
python-tool venv 探测 / probe_tools 全表 / 入库 registry schema 守卫 /
doctor·decompiler 接线。全程 tmp tools_root + 显式 overrides，不触碰宿主
真实安装；涉及 PATH 的用例 monkeypatch shutil.which 保证 hermetic。
"""

import json
import os
from pathlib import Path

import pytest

from core.toolchain import (
    TOOL_KINDS,
    _glob_one,
    load_registry,
    load_tool_overrides,
    probe_tools,
    resolve_tool,
)


def _mk_registry(tools_root: Path, entries: dict) -> Path:
    tools_root.mkdir(parents=True, exist_ok=True)
    p = tools_root / "registry.json"
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return p


# ---------- load_registry：schema 校验 ----------

def test_load_registry_missing(tmp_path):
    assert load_registry(tmp_path / "tools") == {}


def test_load_registry_bad_json(tmp_path):
    t = tmp_path / "tools"
    t.mkdir()
    (t / "registry.json").write_text("{ bad", encoding="utf-8")
    with pytest.raises(ValueError, match="不可读"):
        load_registry(t)


def test_load_registry_top_not_dict(tmp_path):
    t = tmp_path / "tools"
    t.mkdir()
    (t / "registry.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="顶层"):
        load_registry(t)


def test_load_registry_bad_kind(tmp_path):
    t = tmp_path / "tools"
    _mk_registry(t, {"x": {"kind": "nope"}})
    with pytest.raises(ValueError, match="x"):
        load_registry(t)


def test_load_registry_bad_field(tmp_path):
    t = tmp_path / "tools"
    _mk_registry(t, {"x": {"kind": "binary", "search": "nmap.exe"}})  # 须数组
    with pytest.raises(ValueError, match="search"):
        load_registry(t)


def test_load_registry_bad_bin(tmp_path):
    t = tmp_path / "tools"
    _mk_registry(t, {"x": {"kind": "binary", "bin": "runtime/x.exe"}})  # 须平台键对象
    with pytest.raises(ValueError, match="bin"):
        load_registry(t)


def test_load_registry_good(tmp_path):
    t = tmp_path / "tools"
    entries = {"x": {"kind": "binary", "search": ["x.exe"], "fallback": [],
                     "domains": ["pentest"]},
               "y": {"kind": "python-tool", "pip": ["impacket"],
                     "bin": {"windows": "a", "linux": "b"}}}
    _mk_registry(t, entries)
    assert load_registry(t) == entries


# ---------- load_tool_overrides：容错 ----------

def test_overrides_missing(tmp_path):
    assert load_tool_overrides(tmp_path / "nope.json") == {}


def test_overrides_bad_json(tmp_path):
    p = tmp_path / "tools.json"
    p.write_text("{ bad", encoding="utf-8")
    assert load_tool_overrides(p) == {}


def test_overrides_filters_non_dict(tmp_path):
    p = tmp_path / "tools.json"
    p.write_text('{"jadx": {"paths": ["x"]}, "_note": "注释", "top": 3}',
                 encoding="utf-8")
    assert load_tool_overrides(p) == {"jadx": {"paths": ["x"]}}


# ---------- resolve_tool：四来源优先级 ----------

def test_resolve_config_overrides_highest(tmp_path):
    """config 指认压过 bin 规范位 / fallback。"""
    tools = tmp_path / "tools"
    real = tmp_path / "real-jadx" / "jadx.bat"
    real.parent.mkdir(parents=True)
    real.write_text("")
    fb = tmp_path / "fb" / "jadx.bat"
    fb.parent.mkdir(parents=True)
    fb.write_text("")
    entry = {"kind": "binary", "search": ["jadx"], "fallback": [str(fb)]}
    row = resolve_tool("jadx", entry, tools_root=tools,
                       overrides={"jadx": {"paths": [str(real)]}})
    assert row["status"] == "ready" and row["source"] == "config"
    assert Path(row["path"]) == real


def test_resolve_bin_position_bundled_vs_downloaded(tmp_path):
    tools = tmp_path / "tools"
    plat = "windows" if os.name == "nt" else "linux"
    bin_map = {"windows": "runtime/jre/bin/java.exe", "linux": "runtime/jre/bin/java"}
    p = tools / bin_map[plat]
    p.parent.mkdir(parents=True)
    p.write_text("")
    row = resolve_tool("jre", {"kind": "runtime", "acquire": "bundled",
                               "bin": bin_map}, tools_root=tools, overrides={})
    assert row["status"] == "ready" and row["source"] == "bundled"
    row2 = resolve_tool("python", {"kind": "runtime", "bin": bin_map},
                        tools_root=tools, overrides={})
    assert row2["status"] == "ready" and row2["source"] == "downloaded"


def test_resolve_bin_platform_key(tmp_path):
    """bin 平台键取当前平台，`*` 兜底；无键可取则跳过该来源。"""
    tools = tmp_path / "tools"
    p = tools / "data" / "fpdb"
    p.parent.mkdir(parents=True)
    p.write_text("")
    row = resolve_tool("fpdb", {"kind": "data", "bin": {"*": "data/fpdb"}},
                       tools_root=tools, overrides={})
    assert row["status"] == "ready" and row["source"] == "downloaded"
    row2 = resolve_tool("fpdb2", {"kind": "data", "bin": {"linux": "data/x"}},
                        tools_root=tools, overrides={})
    # linux-only 键：Windows 下该来源跳过（Linux 下文件未造同样 missing），不炸
    assert row2["status"] == "missing"


def test_resolve_fallback_beats_path(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    fb = tmp_path / "install" / "ghidra" / "analyzeHeadless.bat"
    fb.parent.mkdir(parents=True)
    fb.write_text("")
    monkeypatch.setattr("core.toolchain.shutil.which",
                        lambda name, path=None: "C:/fake/analyzeHeadless.bat")
    row = resolve_tool("ghidra", {"kind": "binary", "search": ["analyzeHeadless"],
                                  "fallback": [str(fb)]},
                       tools_root=tools, overrides={})
    assert row["source"] == "fallback"
    row2 = resolve_tool("ghidra", {"kind": "binary", "search": ["analyzeHeadless"]},
                        tools_root=tools, overrides={})
    assert row2["source"] == "path" and row2["status"] == "ready"


def test_resolve_missing_with_guide(tmp_path, monkeypatch):
    monkeypatch.setattr("core.toolchain.shutil.which", lambda name, path=None: None)
    row = resolve_tool("nope", {"kind": "binary", "search": ["nope.exe"],
                                "guide": "去某处下载"},
                       tools_root=tmp_path / "tools", overrides={})
    assert row["status"] == "missing" and "去某处下载" in row["detail"]
    assert row["source"] is None and row["path"] is None


# ---------- _glob_one ----------

def test_glob_literal(tmp_path):
    f = tmp_path / "t.exe"
    f.write_text("")
    assert _glob_one(str(f)) == f
    assert _glob_one(str(tmp_path / "missing.exe")) is None


def test_glob_wildcard(tmp_path):
    d = tmp_path / "IDA93"
    d.mkdir()
    f = d / "idat.exe"
    f.write_text("")
    hit = _glob_one((str(tmp_path / "IDA*" / "idat.exe")).replace("\\", "/"))
    assert hit is not None and hit.name == "idat.exe"
    assert _glob_one(str(tmp_path / "IDA*" / "nope.exe").replace("\\", "/")) is None


def test_glob_drive_wildcard(tmp_path):
    """`?:/` 前缀逐盘尝试（C-H），命中 tmp 所在盘。"""
    if tmp_path.drive[0].upper() not in "CDEFGH":
        pytest.skip(f"tmp 盘 {tmp_path.drive} 不在通配盘符集")
    d = tmp_path / "ida93"
    d.mkdir()
    f = d / "idat.exe"
    f.write_text("")
    pattern = f"?:{str(tmp_path)[2:]}/ida93/idat.exe"  # 去盘符拼 ?: 前缀
    assert _glob_one(pattern) == f


# ---------- python-tool venv ----------

def test_python_tool_venv_probe(tmp_path):
    tools = tmp_path / "tools"
    sub = tools / "venv" / ("Scripts" if os.name == "nt" else "bin")
    sub.mkdir(parents=True)
    (sub / ("python.exe" if os.name == "nt" else "python")).write_text("")
    row = resolve_tool("impacket", {"kind": "python-tool", "pip": ["impacket"]},
                       tools_root=tools, overrides={})
    assert row["status"] == "ready" and row["source"] == "bundled"
    assert "venv" in row["detail"]


# ---------- probe_tools 全表 ----------

def test_probe_tools_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr("core.toolchain.shutil.which", lambda name, path=None: None)
    tools = tmp_path / "tools"
    _mk_registry(tools, {
        "bbb": {"kind": "binary", "search": ["bbb.exe"]},
        "aaa": {"kind": "binary"},
    })
    rows = probe_tools(tools, overrides={})
    assert [r["name"] for r in rows] == ["aaa", "bbb"]  # 名称序
    assert all(r["status"] == "missing" for r in rows)


def test_probe_bad_registry_raises(tmp_path):
    t = tmp_path / "tools"
    _mk_registry(t, {"x": {"kind": "nope"}})
    with pytest.raises(ValueError):
        probe_tools(t, overrides={})


def test_repo_registry_validates():
    """入库 registry.json schema 守卫（真文件只读校验，零探测副作用）。"""
    reg = load_registry(Path(__file__).resolve().parent.parent / "tools")
    assert "ida" in reg and "ghidra" in reg and "fpdb" in reg
    # android 三件（android-kb-sourcing M2）：bundled python-tool，bin 指向随仓脚本
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    for name in ("android-ctf-runner", "godot-ctf-runner", "android-unpack-kit"):
        e = reg[name]
        assert e["kind"] == "python-tool" and e["acquire"] == "bundled"
        # bin 相对 tools_root（tools/）解析，不是 CWD
        assert (tools_dir / e["bin"]["*"]).is_file(), f"{name} 的 bin 规范位文件缺失"
    assert {v["kind"] for v in reg.values()} <= set(TOOL_KINDS)


# ---------- doctor 接线 ----------

def test_doctor_toolchain_issues(tmp_path, monkeypatch):
    from core.skills.doctor import diagnose
    monkeypatch.setattr("core.toolchain.shutil.which", lambda name, path=None: None)
    packs = tmp_path / "packs"
    packs.mkdir()
    tools = tmp_path / "tools"
    _mk_registry(tools, {"zap-xyz": {"kind": "binary", "search": ["zap-xyz.exe"],
                                     "guide": "装一下"}})
    rep = diagnose(packs, tools_root=tools)
    miss = [i for i in rep.issues if i.code == "tool-missing"]
    assert len(miss) == 1 and miss[0].level == "info"
    assert "zap-xyz" in miss[0].target and "装一下" in miss[0].message

    # 坏结构 → error
    (tools / "registry.json").write_text('{"x": {"kind": "nope"}}', encoding="utf-8")
    rep2 = diagnose(packs, tools_root=tools)
    bad = [i for i in rep2.issues if i.code == "tool-registry-invalid"]
    assert len(bad) == 1 and bad[0].level == "error"

    # 不传 tools_root → 不出工具体检
    rep3 = diagnose(packs)
    assert not [i for i in rep3.issues if i.code.startswith("tool-")]


# ---------- decompiler 接线 ----------

def test_decompiler_resolve_registry_first(monkeypatch, tmp_path):
    from core.tools import decompiler
    fake_ida = tmp_path / "ida" / "idat.exe"
    fake_ida.parent.mkdir(parents=True)
    fake_ida.write_text("")
    fake_gh = tmp_path / "gh" / "analyzeHeadless.bat"
    fake_gh.parent.mkdir(parents=True)
    fake_gh.write_text("")
    monkeypatch.setattr("core.toolchain.load_registry", lambda tools_root="tools": {
        "ida": {"kind": "binary", "fallback": [str(fake_ida)]},
        "ghidra": {"kind": "binary", "fallback": [str(fake_gh)]},
    })
    assert decompiler.resolve_ida_headless() == str(fake_ida)
    assert decompiler.resolve_ghidra_headless() == str(fake_gh)


def test_decompiler_resolve_ghidra_path_fallback(monkeypatch):
    """registry 无条目/读取失败 → PATH 兜底，绝不抛异常。"""
    from core.tools import decompiler
    monkeypatch.setattr("core.toolchain.load_registry", lambda tools_root="tools": {})
    monkeypatch.setattr(decompiler.shutil, "which",
                        lambda name, path=None: "C:/x/analyzeHeadless.bat")
    assert decompiler.resolve_ghidra_headless() == "C:/x/analyzeHeadless.bat"


def test_decompiler_factory_ghidra_uses_registry(monkeypatch, tmp_path):
    """工厂 ghidra 腿经 resolve_ghidra_headless 拿到 registry 命中路径。"""
    from core.tools import decompiler
    fake_gh = tmp_path / "gh" / "analyzeHeadless.bat"
    fake_gh.parent.mkdir(parents=True)
    fake_gh.write_text("")
    monkeypatch.setattr("core.toolchain.load_registry", lambda tools_root="tools": {
        "ghidra": {"kind": "binary", "fallback": [str(fake_gh)]}})
    monkeypatch.setattr(decompiler.shutil, "which", lambda name, path=None: None)
    svc = decompiler.build_headless_service(tmp_path / "cache", runner=lambda *a, **k: None,
                                            prefer=("ghidra",), available=True)
    assert svc.backends and svc.backends[0].headless_cmd == str(fake_gh)
