"""工作区隔离与产物统一归置（W1-W4）测试：pathguard 静态扫描、网关硬拒与
cwd/TEMP 重定向、产物归属元数据、scratch 清理端点。"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from core.blackboard.schema import SCHEMA_VERSION  # noqa: E402
from core.blackboard.store import Blackboard  # noqa: E402
from core.runtime import pathguard  # noqa: E402
from core.runtime.backends import NativeBackend  # noqa: E402
from core.runtime.gateway import ExecutionGateway, GatewayDenied  # noqa: E402


# ---------- pathguard 纯函数 ----------

WS = r"E:\proj\ws"
SCRATCH = WS + r"\scratch"


def _clean_out(t: str) -> str:
    return t.strip().strip("\"'")


def test_scan_redirection_targets():
    toks = pathguard.scan_write_targets("Get-Content a.txt > out.txt")
    assert "out.txt" in [_clean_out(t) for t in toks]
    assert pathguard.scan_write_targets("dir >> log.txt 2> err.txt")


def test_scan_curl_output():
    assert any("x.json" in t for t in pathguard.scan_write_targets(
        'curl.exe -s -o "$env:TEMP\\x.json" https://a.b'))
    assert any("y.bin" in t for t in pathguard.scan_write_targets(
        "wget -O y.bin http://a.b"))
    assert any("z.csv" in t for t in pathguard.scan_write_targets(
        "tool --output=z.csv"))


def test_scan_ps_cmdlets_and_tee():
    assert any("a.txt" in t for t in pathguard.scan_write_targets(
        "scan | Out-File a.txt"))
    assert any("b.log" in t for t in pathguard.scan_write_targets(
        "run | Tee-Object b.log"))
    assert any("c.list" in t for t in pathguard.scan_write_targets(
        "scan | tee c.list"))


def test_scan_false_positives():
    # 引号内的 > 不是重定向目标；URL 的路径段不构成写目标
    assert pathguard.scan_write_targets('echo "a>b"') == []


def test_scan_grep_only_matching_not_write():
    """grep 族 -o*（only-matching）是输出开关不写文件，模式串/文件名不被当
    写目标（2026-09-23 误报修复）；curl/nmap 风格 -o 写文件仍拦。"""
    assert pathguard.scan_write_targets("grep -oE '\"session_id\":\"[^\"]*\"' spill.json") == []
    assert pathguard.scan_write_targets("cat spill.json | grep -o pat") == []
    assert pathguard.scan_write_targets("cat spill.json|grep -oE pat") == []  # 连写形态
    assert pathguard.scan_write_targets("rg --only-matching 'pat' f.json") == []
    assert pathguard.scan_write_targets("grep.exe -o pat f.log") == []
    # 写文件的 -o 仍拦：nmap -oG / curl -o
    assert any("out.gnmap" in t for t in pathguard.scan_write_targets("nmap -oG out.gnmap x"))
    assert any("o.json" in t for t in pathguard.scan_write_targets("curl -o o.json http://a.b"))


def test_escapes_grep_pattern_allowed_redirect_still_enforced():
    """实战误报场景（sess-da36bf37d645）：grep -oE 模式串（前导 /）不再判逃逸；
    grep 的重定向写目标照常拦。"""
    assert pathguard.workspace_escapes(
        'grep -oE "/api/v[0-9]+/" spill.json', scratch=SCRATCH, workspace=WS) == []
    assert pathguard.workspace_escapes(
        "grep -o pat > D:\\evil.txt", scratch=SCRATCH, workspace=WS)


def test_escapes_absolute_outside():
    esc = pathguard.workspace_escapes(
        "curl.exe -o C:\\Temp\\evil.bin http://a.b", scratch=SCRATCH, workspace=WS)
    assert esc and "evil.bin" in esc[0]


def test_escapes_tilde_and_updir():
    assert pathguard.workspace_escapes("tool > ~/x", scratch=SCRATCH, workspace=WS)
    assert pathguard.workspace_escapes("echo x > ..\\..\\escape.txt",
                                       scratch=SCRATCH, workspace=WS)
    assert pathguard.workspace_escapes("run > /etc/evil", scratch=SCRATCH, workspace=WS)


def test_allows_relative_and_inside_workspace():
    assert pathguard.workspace_escapes("curl -o probe.json http://a.b",
                                       scratch=SCRATCH, workspace=WS) == []
    assert pathguard.workspace_escapes("echo x > work.txt",
                                       scratch=SCRATCH, workspace=WS) == []
    assert pathguard.workspace_escapes(f"echo x > {SCRATCH}\\sub\\out.txt",
                                       scratch=SCRATCH, workspace=WS) == []
    # 写 workspace 内其他目录（如 .tmp）也放行
    assert pathguard.workspace_escapes(f"tool > {WS}\\.tmp\\x.txt",
                                       scratch=SCRATCH, workspace=WS) == []


def test_allows_variable_and_devnull_targets():
    # $env:TEMP 已被网关重定向进项目；变量间接无法静态解析 → 放行
    assert pathguard.workspace_escapes('curl -o "$env:TEMP\\x" http://a.b',
                                       scratch=SCRATCH, workspace=WS) == []
    assert pathguard.workspace_escapes("run > /dev/null", scratch=SCRATCH,
                                       workspace=WS) == []
    assert pathguard.workspace_escapes("run 2>&1 > log.txt", scratch=SCRATCH,
                                       workspace=WS) == []


def test_allows_devnull_with_glued_separator():
    """shell 习惯写法 2>/dev/null;（分隔符无空格）整串是一个 token——目标必须在
    引号外的 ; | & 处截断，否则 /dev/null; 被当绝对路径误判逃逸
    （2026-09-25 实战 23/43 条误拦实锤）。"""
    assert pathguard.scan_write_targets("wc -c f 2>/dev/null; echo x",
                                        posix=True) == ["/dev/null"]
    assert pathguard.workspace_escapes(
        "wc -c f 2>/dev/null; head f 2>/dev/null;",
        scratch="/mnt/e/proj/ws/scratch", workspace="/mnt/e/proj/ws",
        posix=True) == []
    assert pathguard.workspace_escapes(
        "ls f 2>/dev/null && grep x f 2>/dev/null",
        scratch="/mnt/e/proj/ws/scratch", workspace="/mnt/e/proj/ws",
        posix=True) == []
    # 真逃逸仍拦：写 /tmp
    assert pathguard.workspace_escapes(
        "curl -o /tmp/x http://a; echo",
        scratch="/mnt/e/proj/ws/scratch", workspace="/mnt/e/proj/ws",
        posix=True) == ["/tmp/x"]
    # 引号内分隔符不切（引号感知）
    assert pathguard.scan_write_targets('echo x >"a;b.txt"', posix=True) == \
        ['"a;b.txt"']


def test_allows_devnull_with_subshell_paren():
    """`$(curl … 2>/dev/null)` 子壳右括号紧贴重定向目标是 bash 高频惯用法——
    目标必须在引号外的 `)` 处截断，否则提取出伪路径 /dev/null) 绕过 /dev/null
    白名单被误判逃逸（2026-09-29 实战 sess-948e9ba771eb：stderr 丢弃三连拒致
    E2 熔断挂起实锤；模型照回执改对输出文件仍拒，原样重试三振出局）。"""
    cmd = ("for p in actuator actuator/health; do "
           "code=$(curl -sS -m 8 -o xj_tmp.txt -w '%{http_code}' "
           "http://xjapi.zut.edu.cn/$p 2>/dev/null); "
           "size=$(wc -c < xj_tmp.txt 2>/dev/null); "
           "echo \"$p $code\"; done; rm -f xj_tmp.txt")
    assert "/dev/null)" not in pathguard.scan_write_targets(cmd, posix=True)
    assert pathguard.workspace_escapes(
        cmd, scratch="/workspace/scratch", workspace="/workspace",
        posix=True) == []
    # 裸子壳（无 $）同形态
    assert pathguard.workspace_escapes(
        "(curl -sS http://a.b 2>/dev/null) | head -3",
        scratch="/workspace/scratch", workspace="/workspace",
        posix=True) == []
    # 同一逃逸目标多次出现只报一次（回执不重复抖串）
    assert pathguard.workspace_escapes(
        "curl -o /tmp/a http://x 2>/dev/null); echo x > /tmp/a 2>/dev/null)",
        scratch="/workspace/scratch", workspace="/workspace",
        posix=True) == ["/tmp/a"]
    # 真逃逸带括号照样拦：截断只会让提取更准（/tmp/x) → /tmp/x），无新旁路
    assert pathguard.workspace_escapes(
        "curl -o /tmp/x http://a 2>/dev/null)",
        scratch="/workspace/scratch", workspace="/workspace",
        posix=True) == ["/tmp/x"]


def test_escapes_posix_wsl():
    esc = pathguard.workspace_escapes(
        "nmap -oG /tmp/scan.gnmap x", scratch="/mnt/e/proj/ws/scratch",
        workspace="/mnt/e/proj/ws", posix=True)
    assert esc
    ok = pathguard.workspace_escapes("nmap -oG out.gnmap x",
                                     scratch="/mnt/e/proj/ws/scratch",
                                     workspace="/mnt/e/proj/ws", posix=True)
    assert ok == []


def test_windows_to_wsl_path():
    assert pathguard.windows_to_wsl_path(r"E:\proj\ws\scratch") == \
        "/mnt/e/proj/ws/scratch"


# ---------- 网关集成（真实 host 后端，Windows 语义） ----------

@pytest.fixture()
def ws_dir(tmp_path):
    return tmp_path / "ws"


def _gateway(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    return ExecutionGateway(bb=bb), bb


@pytest.mark.skipif(os.name != "nt", reason="host 后端 Windows 语义")
def test_gateway_denies_outside_write(tmp_path, ws_dir):
    gw, bb = _gateway(tmp_path)
    pid = bb.create_project("p1", track="ctf")["id"]
    with pytest.raises(GatewayDenied) as ei:
        gw.run("echo hi > C:\\__wsguard_evil.txt", "host",
               project_id=pid, threat_class="trusted", workspace=ws_dir)
    assert "工作区隔离" in str(ei.value)
    kinds = [e["kind"] for e in bb.recent_events(pid, 0, 100)]
    assert "audit.deny" in kinds
    assert not Path("C:\\__wsguard_evil.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="host 后端 Windows 语义")
def test_gateway_cwd_and_temp_redirect(tmp_path, ws_dir):
    gw, bb = _gateway(tmp_path)
    pid = bb.create_project("p1", track="ctf")["id"]
    r = gw.run("Set-Content -Path rel.txt -Value data; Write-Output $env:TEMP",
               "host", project_id=pid, threat_class="trusted", workspace=ws_dir)
    assert r.exit_code == 0
    assert (ws_dir / "scratch" / "rel.txt").exists()
    assert str(ws_dir / ".tmp") in r.stdout
    evs = [e for e in bb.recent_events(pid, 0, 100) if e["kind"] == "command"]
    assert evs and evs[-1]["payload"].get("cwd", "").endswith("scratch")


@pytest.mark.skipif(os.name != "nt", reason="host 后端 Windows 语义")
def test_gateway_no_workspace_keeps_legacy_behavior(tmp_path):
    gw, _ = _gateway(tmp_path)
    r = gw.run("Write-Output ok", "host", threat_class="trusted")
    assert r.exit_code == 0 and "ok" in r.stdout


def test_native_backend_rejects_escape_without_exec(tmp_path, ws_dir):
    """pathguard 拒绝发生在 backend 执行前：恶意写目标命令不会真的跑。"""
    calls = []
    backend = NativeBackend()
    orig = backend.execute

    def spy(cmd, timeout=120.0, cwd=None, env=None):
        calls.append(cmd)
        return orig(cmd, timeout, cwd, env)

    backend.execute = spy  # type: ignore[method-assign]
    gw = ExecutionGateway(backends={"host": backend})
    with pytest.raises(GatewayDenied):
        gw.run("echo x > D:\\evil.txt", "host", workspace=ws_dir)
    assert calls == []  # 被拒命令从未到达后端


# ---------- 产物归属元数据（W3，schema v10） ----------

def test_artifact_attribution_and_filter(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    assert SCHEMA_VERSION >= 10  # W3 v10 artifacts.meta；F11 起可能更高（v11 findings.rating_basis）
    pid = bb.create_project("p1", track="ctf")["id"]
    aid = bb.add_artifact(pid, "file/poc.py", kind="file", sha256="ab" * 32,
                          author="sess-x", meta={"session_id": "sess-x",
                                                 "task_id": "task-1"})
    bb.add_artifact(pid, "file/other.txt", kind="file", author="human")
    arts = bb.list_artifacts(pid)
    assert len(arts) == 2
    by_task = bb.list_artifacts(pid, task_id="task-1")
    assert len(by_task) == 1 and by_task[0]["id"] == aid
    by_sess = bb.list_artifacts(pid, session_id="sess-x")
    assert len(by_sess) == 1
    assert bb.list_artifacts(pid, task_id="task-none") == []


def test_artifact_meta_migration_on_old_db(tmp_path):
    """v9 旧库（artifacts 无 meta 列）打开后幂等补列。"""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT '',
            config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
        CREATE TABLE artifacts(id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            path TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'file',
            description TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key, value) VALUES('schema_version', '9');
        """
    )
    conn.commit()
    conn.close()
    bb = Blackboard(str(db))
    pid = bb.create_project("m1", track="ctf")["id"]
    aid = bb.add_artifact(pid, "file/a.txt", meta={"task_id": "t1"})
    arts = bb.list_artifacts(pid, task_id="t1")
    assert len(arts) == 1 and arts[0]["id"] == aid
