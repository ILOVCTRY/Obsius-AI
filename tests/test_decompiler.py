"""反编译组合服务测试（不触网、不依赖真实 Ghidra/IDA）。

覆盖：headless 导出缓存、MCP 在线优先、不可用引导文本、
Agent 工具面的 func_kb 机制级查重。
"""

import copy
import json
import os
import threading
import time

import pytest

from pathlib import Path

from core.agent.tools import ToolDispatcher
from core.blackboard import Blackboard
import core.tools.decompiler as dc
from core.tools.decompiler import (
    DecompilerService,
    GhidraHeadlessBackend,
    IDAHeadlessBackend,
    MCPBackend,
    build_headless_service,
    build_strings,
    build_xrefs,
    diff_pulled_names,
    is_auto_name,
    is_partial_export,
    sha256_file,
)

EXPORT = {
    "export_version": 3,
    "binary": "crackme.elf",
    "meta": {"arch": "x86-64", "bits": 64, "endian": "le",
             "imagebase": "0x0", "entry": "0x1234", "filename": "crackme.elf"},
    "sections": [{"name": ".text", "vaddr": "0x1000", "size": 4096,
                  "perms": "r-x", "entropy": 5.123}],
    "imports": {"libc.so.6": ["strlen", "puts", "fgets"]},
    "functions": [
        {"address": 0x1189, "name": "check_flag", "size": 96,
         "calls": ["strlen", "puts"],
         "pseudocode": "int check_flag(char *input) {\n  if (strlen(input) != 0x15) return 0;\n  ...}"},
        {"address": 0x1234, "name": "main", "size": 200,
         "calls": ["check_flag", "fgets"],
         "pseudocode": "int main() { fgets(...); if (check_flag(buf)) puts(\"Correct\"); }"},
    ],
    "strings": [
        {"address": 0x2000, "string": "Enter key: ", "length": 12, "type": "cstr",
         "refs": [{"func_addr": 0x1234, "from_addr": 0x1240}]},
        {"address": 0x2010, "string": "Correct!", "length": 8, "type": "cstr",
         "refs": [{"func_addr": 0x1234, "from_addr": 0x1250},
                  {"func_addr": 0x1189, "from_addr": 0x11A0}]},
        {"address": 0x2020, "string": "密钥", "length": 1, "type": "unicode", "refs": []},
    ],
}


@pytest.fixture()
def sample(tmp_path):
    p = tmp_path / "sample.elf"
    p.write_bytes(b"\x7fELF" + b"\x00" * 100)
    return p


def fake_ghidra_runner(tmp_path, calls=None):
    """假 analyzeHeadless：解析 -postScript 后的 out 路径，写出 canned JSON。"""
    if calls is None:
        calls = {"n": 0}

    def run(args):
        calls["n"] += 1
        if "-postScript" in args:
            out = Path_arg(args[args.index("-postScript") + 2])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(EXPORT), encoding="utf-8")
            return 0, f"exported {len(EXPORT['functions'])} functions", ""
        return 1, "", "unknown invocation"
    from pathlib import Path as Path_arg
    return run, calls


def make_service(tmp_path, sample, ghidra_runner=None):
    g = GhidraHeadlessBackend(runner=ghidra_runner, available=True,
                              tmp_project_dir=tmp_path / "ghidra-tmp")
    return DecompilerService(cache_dir=tmp_path / "cache", ghidra=g)


def test_headless_export_and_cache(tmp_path, sample):
    runner, calls = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    syms = json.loads(svc.list_functions(str(sample)))
    assert [s["name"] for s in syms] == ["check_flag", "main"]
    assert calls["n"] == 1
    # 第二次走缓存，不再执行 headless
    svc.list_functions(str(sample))
    assert calls["n"] == 1
    # 按地址点查
    text = svc.decompile(str(sample), address=0x1189)
    assert text.startswith("== check_flag @ 0x1189 ==") and "strlen" in text
    # 全量概览（截断防淹没）
    overview = svc.decompile(str(sample))
    assert overview.count("== ") == 2


# ---------- Agent 检索面（2026-09-27：strings_for / xrefs_for_func / list_functions 过滤） ----------

def test_list_functions_filters(tmp_path, sample):
    """list_functions 过滤参数：按名（大小写不敏感）/按大小筛；无命中回空表。"""
    runner, _calls = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    by_name = json.loads(svc.list_functions(str(sample), name_contains="FLAG"))
    assert [s["name"] for s in by_name] == ["check_flag"]
    by_size = json.loads(svc.list_functions(str(sample), min_size=150))
    assert [s["name"] for s in by_size] == ["main"]
    assert json.loads(svc.list_functions(str(sample), name_contains="zzz")) == []


def test_strings_for_filter_and_limit(tmp_path, sample):
    """strings_for：子串过滤带地址+引用函数；limit 截断计数；无后端回引导文本。"""
    runner, _calls = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    hit = json.loads(svc.strings_for(str(sample), q="correct"))
    assert hit["count"] == 1 and hit["truncated"] is False
    row = hit["items"][0]
    assert row["address"] == "0x2010" and row["string"] == "Correct!"
    assert row["refs"][0]["func_name"] == "main"
    capped = json.loads(svc.strings_for(str(sample), limit=2))
    assert capped["count"] == 2 and capped["total_matched"] == 3
    assert capped["truncated"] is True
    bare = DecompilerService(cache_dir=tmp_path / "c2")
    assert bare.strings_for(str(sample)).startswith("[反编译器不可用]")


def test_xrefs_for_func_name_address_and_unknown(tmp_path, sample):
    """xrefs_for_func：name 直查 / address 先解析成函数名 / 未知地址回指引。"""
    runner, _calls = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    by_name = json.loads(svc.xrefs_for_func(str(sample), name="check_flag"))
    assert by_name["callers"] == ["main"]
    assert by_name["callees"] == ["strlen", "puts"]
    by_addr = json.loads(svc.xrefs_for_func(str(sample), address=0x1189))
    assert by_addr["function"] == "check_flag"
    miss = svc.xrefs_for_func(str(sample), address=0x9999)
    assert miss.startswith("[错误]") and "list_symbols" in miss


# ---------- MCP 实时桥（streamable-http 会话 / 真机工具别名 / 降级） ----------

class FakeMCPTransport:
    """可编程 MCP streamable-http 假传输（不触网）。

    routes: {真机工具名: fn(args, transport) -> payload | (status, payload)}；
    payload 为 dict/list，按真机 zeromcp 约定 json.dumps 进 content[0].text。
    记录每次调用 (method, params, headers) 供断言 session 头/参数形状。
    """

    DEFAULT_TOOLS = ["decompile", "list_funcs", "rename", "set_comments",
                     "func_profile", "entity_query", "server_health", "idb_save",
                     "analyze_batch"]

    def __init__(self, routes=None, *, tools=None, session_header=True, session="sess-abc"):
        self.routes = routes or {}
        self.tools = tools if tools is not None else self.DEFAULT_TOOLS
        self.session_header = session_header
        self.session = session
        self.calls = []
        self.init_count = 0

    def __call__(self, url, body, timeout, headers=None):
        headers = headers or {}
        method = body["method"]
        self.calls.append((method, body.get("params"), headers))
        if method == "initialize":
            self.init_count += 1
            h = {"Mcp-Session-Id": self.session} if self.session_header else {}
            return 200, {"jsonrpc": "2.0", "id": body.get("id"),
                         "result": {"protocolVersion": "2024-11-05",
                                    "serverInfo": {"name": "ida-pro-mcp"}}}, h
        if method == "notifications/initialized":
            assert headers.get("Mcp-Session-Id") == self.session
            return 202, None, {}
        if method == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": body.get("id"),
                         "result": {"tools": [{"name": n} for n in self.tools]}}, {}
        if method == "tools/call":
            name = body["params"]["name"]
            args = body["params"]["arguments"]
            handler = self.routes.get(name)
            if handler is None:
                return 200, {"jsonrpc": "2.0", "id": body.get("id"),
                             "result": {"isError": True,
                                        "content": [{"type": "text", "text": "no canned"}]}}, {}
            out = handler(args, self)
            if isinstance(out, tuple):  # (status, payload)：模拟 4xx session 失效
                status, payload = out
                return status, {"jsonrpc": "2.0", "id": body.get("id"),
                                "error": {"code": -32001, "message": "bad session"}}, {}
            return 200, {"jsonrpc": "2.0", "id": body.get("id"),
                         "result": {"content": [{"type": "text",
                                                 "text": json.dumps(out)}]}}, {}
        return 0, None, {}


def _dead_transport(url, body, timeout, headers=None):
    return 0, None, {}


def _mcp_service(tmp_path, transport, *, ghidra=False, runner=None):
    mcp = MCPBackend(transport=transport)
    svc = DecompilerService(cache_dir=tmp_path / "cache", mcp_endpoint=None)
    backends = [mcp]
    if ghidra:
        backends.append(GhidraHeadlessBackend(runner=runner, available=True,
                                              tmp_project_dir=tmp_path / "ghidra-tmp"))
    svc.backends = backends
    svc.mcp = mcp
    return svc, mcp


def test_mcp_online_preferred_over_headless(tmp_path, sample):
    """MCP 在线 → 点查走 MCP（带 Mcp-Session-Id），headless runner 一次都不跑。"""
    transport = FakeMCPTransport(routes={
        "decompile": lambda args, t: {"addr": args["addr"], "code": "int main() {...}"}})
    runner, calls = fake_ghidra_runner(tmp_path)
    svc, mcp = _mcp_service(tmp_path, transport, ghidra=True, runner=runner)
    text = svc.decompile(str(sample), address=0x1234)
    assert text == "int main() {...}" and calls["n"] == 0
    methods = [c[0] for c in transport.calls]
    assert methods[0] == "initialize"
    # initialize 不带 session 头；initialized 与 tools/call 必须回带响应头给的 id
    assert transport.calls[0][2].get("Mcp-Session-Id") is None
    call_req = next(c for c in transport.calls if c[0] == "tools/call")
    assert call_req[2]["Mcp-Session-Id"] == "sess-abc"
    assert call_req[1]["name"] == "decompile"
    assert call_req[1]["arguments"] == {"addr": "0x1234"}
    # MCP 掉线后降级 headless 缓存
    mcp._transport = _dead_transport
    mcp._health = None
    text = svc.decompile(str(sample), address=0x1189)
    assert "check_flag" in text and calls["n"] == 1


def test_mcp_health_ttl_caches_handshake(tmp_path):
    """available() TTL 内不重复握手；TTL 过期后重新握手。"""
    transport = FakeMCPTransport()
    mcp = MCPBackend(transport=transport, health_ttl=3.0)
    assert mcp.available() is True and transport.init_count == 1
    assert mcp.available() is True and transport.init_count == 1  # TTL 内
    assert mcp.available() is True  # 直接复用 session，不再 initialize
    mcp._health_at -= 10.0
    assert mcp.available() is True and transport.init_count == 2
    # 离线：负缓存也走 TTL
    mcp._transport = _dead_transport
    mcp._health = None
    assert mcp.available() is False
    assert mcp.available() is False and transport.init_count == 2


def test_mcp_rehandshake_on_4xx(tmp_path, sample):
    """tools/call 撞 4xx（旧 session 失效）→ 清 session 重新握手并重试一次。"""
    state = {"n": 0}

    def decompile(args, t):
        state["n"] += 1
        if state["n"] == 1:
            return (400, None)
        return {"addr": args["addr"], "code": "int live(){...}"}

    transport = FakeMCPTransport(routes={"decompile": decompile})
    mcp = MCPBackend(transport=transport)
    assert mcp.decompile_at(0x1189) == "int live(){...}"
    assert transport.init_count == 2  # 初次握手 + 4xx 后重握
    assert state["n"] == 2            # 工具调了两次（失败一次 + 重试一次）


def test_mcp_writeback_batch_shape_and_channel(tmp_path):
    """实时写回：rename batch + set_comments items 参数形状对齐真机，applied 统计。"""
    seen = {}

    def rename(args, t):
        seen["rename"] = args
        return {"func": [{"addr": "0x1189", "old": "sub_1189",
                          "name": "check_flag", "ok": True}],
                "summary": {"ok": 1, "failed": 0}}

    def comments(args, t):
        seen["comments"] = args
        return [{"addr": "0x1189", "ok": True}]

    mcp = MCPBackend(transport=FakeMCPTransport(routes={"rename": rename,
                                                        "set_comments": comments}))
    res = mcp.writeback_items([{"address": 0x1189, "name": "check_flag",
                                "comment": "XOR 0x5A"}])
    assert res["status"] == "ok" and res["channel"] == "mcp" and res["applied"] == 2
    # 真机 rename(batch: RenameBatch)：func/allow_overwrite 必须包在 batch 里
    assert seen["rename"] == {"batch": {"func": [{"addr": "0x1189", "name": "check_flag"}],
                                        "allow_overwrite": True}}
    assert seen["comments"] == {"items": [{"addr": "0x1189", "comment": "XOR 0x5A"}]}
    # 只有名字：不调 set_comments
    seen.clear()
    mcp2 = MCPBackend(transport=FakeMCPTransport(routes={"rename": rename}))
    res2 = mcp2.writeback_items([{"address": "0x1189", "name": "check_flag"}])
    assert res2["applied"] == 1 and "comments" not in seen
    # 离线 → None（交给 headless 降级）
    dead = MCPBackend(transport=_dead_transport)
    assert dead.writeback_items([{"address": 1, "name": "x"}]) is None


def test_mcp_xref_uses_func_profile_alias(tmp_path):
    """xrefs 别名修 KeyError：走 func_profile(include_lists) 取 callers/callees。"""
    def profile(args, t):
        q = args["queries"][0]
        assert q["include_lists"] is True and q["max_items"] == 100
        assert q["query"] in ("0x1189", "check_flag")
        return [{"query": q["query"], "error": None, "data": [{
            "addr": "0x1189", "name": "check_flag",
            "callers": [{"addr": "0x1234", "name": "main"}],
            "callees": [{"addr": "0x1050", "name": "strlen"}]}]}]

    mcp = MCPBackend(transport=FakeMCPTransport(routes={"func_profile": profile}))
    x = mcp.xref_profile(0x1189)
    assert x == {"address": "0x1189", "name": "check_flag",
                 "callers": [{"address": "0x1234", "name": "main"}],
                 "callees": [{"address": "0x1050", "name": "strlen"}],
                 "source": "mcp"}
    # Agent xrefs() 旧路径（backend._call_tool("xrefs") 必 KeyError 的回归）
    svc, _ = _mcp_service(tmp_path, mcp._transport)
    out = json.loads(svc.xrefs("whatever.elf", "check_flag"))
    assert out["callers"] == ["main"] and out["callees"] == ["strlen"]


def test_mcp_loopback_guard_and_endpoint_select():
    """红线：非 loopback 端点拒绝构造；配置选路 domains/transport/url 过滤 + 默认兜底。"""
    import pytest
    with pytest.raises(ValueError):
        MCPBackend("http://10.0.0.1:13337/mcp")
    with pytest.raises(ValueError):
        MCPBackend("http://192.168.1.5:13337/mcp")
    select = dc.select_mcp_endpoint
    assert select(None) == dc.MCP_DEFAULT_ENDPOINT
    assert select({}) == dc.MCP_DEFAULT_ENDPOINT
    # 显式 reverse http loopback → 用它
    cfg = {"servers": [{"name": "ida", "transport": "streamable-http",
                        "url": "http://127.0.0.1:13337/mcp", "enabled": True,
                        "domains": ["reverse"]}]}
    assert select(cfg) == "http://127.0.0.1:13337/mcp"
    # binary 旧域标兼容
    cfg["servers"][0]["domains"] = ["binary"]
    assert select(cfg) == "http://127.0.0.1:13337/mcp"
    # domains 不命中（pentest）/ disabled / stdio / 非 loopback → 全部回默认
    assert select({"servers": [{**cfg["servers"][0], "domains": ["pentest"]}]}) \
        == dc.MCP_DEFAULT_ENDPOINT
    assert select({"servers": [{**cfg["servers"][0], "domains": ["reverse"],
                                "enabled": False}]}) == dc.MCP_DEFAULT_ENDPOINT
    assert select({"servers": [{**cfg["servers"][0], "domains": ["reverse"],
                                "transport": "stdio",
                                "command": "x"}]}) == dc.MCP_DEFAULT_ENDPOINT
    assert select({"servers": [{"name": "ida", "transport": "streamable-http",
                                "url": "http://10.1.2.3:13337/mcp",
                                "domains": ["reverse"]}]}) == dc.MCP_DEFAULT_ENDPOINT


def test_mcp_parse_body_json_sse_plain():
    parse = dc._parse_mcp_body
    assert parse('{"jsonrpc":"2.0","result":{}}') == {"jsonrpc": "2.0", "result": {}}
    sse = ('event: message\ndata: {"jsonrpc":"2.0","id":1}\n\n'
           'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')
    assert parse(sse)["result"] == {"ok": True}
    assert parse("Accepted") is None and parse("") is None


def test_service_writeback_prefers_mcp_then_headless(tmp_path):
    """MCP 在线 writeback 走实时通道；传输失败才降级 headless apply_by_sha。"""
    transport = FakeMCPTransport(routes={
        "rename": lambda a, t: {"func": [{"addr": a["batch"]["func"][0]["addr"], "ok": True}]},
        "set_comments": lambda a, t: [{"addr": a["items"][0]["addr"], "ok": True}]})
    svc, mcp = _mcp_service(tmp_path, transport)
    res = svc.writeback("sha", [{"address": 0x1189, "name": "check_flag"}])
    assert res["status"] == "ok" and res["channel"] == "mcp" and res["applied"] == 1
    # 掉线：装个假 IDA backend 验证降级 apply_by_sha 被调
    ida = IDAHeadlessBackend(runner=lambda args: (0, "", ""), available=True)
    ida.apply_by_sha = lambda sha, items: {"status": "ok", "applied": 1, "results": []}
    svc.backends.append(ida)
    mcp._transport = _dead_transport
    mcp._health = None
    res = svc.writeback("sha", [{"address": 0x1189, "name": "check_flag"}])
    assert res["status"] == "ok" and "channel" not in res and res["applied"] == 1


def test_service_live_decompile_and_xref_cache_absent(tmp_path):
    """缓存缺席：MCP 在线实时取（source=mcp）；离线 None（API 据此回 409）。"""
    transport = FakeMCPTransport(routes={
        "decompile": lambda a, t: {"addr": a["addr"], "code": "// live\nint f(){}"},
        "func_profile": lambda a, t: [{"data": [{
            "addr": "0x1189", "name": "f", "callers": [], "callees": []}]}]})
    svc, _ = _mcp_service(tmp_path, transport)
    live = svc.live_decompile(0x1189)
    assert live == {"source": "mcp", "address": "0x1189",
                    "pseudocode": "// live\nint f(){}"}
    x = svc.xrefs_for("nonexistent-sha", 0x1189)  # 无缓存 → MCP 降级
    assert x["source"] == "mcp" and x["name"] == "f"
    svc.mcp._transport = _dead_transport
    svc.mcp._health = None
    assert svc.live_decompile(0x1189) is None
    assert svc.xrefs_for("nonexistent-sha", 0x1189) is None


def test_no_backend_returns_guidance(tmp_path, sample):
    svc = DecompilerService(cache_dir=tmp_path / "cache")  # 什么都不装
    text = svc.decompile(str(sample), address=0x1189)
    assert text.startswith("[反编译器不可用]") and "winget" in text


def test_annotate_falls_back_to_sidecar(tmp_path, sample):
    runner, _ = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    out = svc.annotate(str(sample), "check_flag", "XOR 0x5A 校验")
    assert "sidecar" in out
    sidecar = tmp_path / "cache" / f"{sha256_file(sample)}.annotations.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))[0]["name"] == "check_flag"


# ---------- rev 工作台：v3 契约 / 工厂选序 / read_cached / xref / strings ----------

def fake_ida_runner(calls=None):
    """假日 idat：从 -S"<script> <out>" 抠出 JSON 路径写 canned v3，并按 -o 主干产 .i64。"""
    if calls is None:
        calls = {"n": 0}

    def run(args):
        calls["n"] += 1
        out = None
        stem = None
        for a in args:
            if a.startswith('-S"'):
                out = Path(a[3:].rstrip('"').rsplit(" ", 1)[1])
            elif a.startswith("-o"):
                stem = Path(a[2:])
        if out is None:
            return 1, "", "no -S"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(EXPORT), encoding="utf-8")
        if stem is not None:
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_suffix(".i64").write_bytes(b"IDADB")
        return 0, "idat ok", ""

    return run, calls


def test_stale_cache_invalidated_and_reexported(tmp_path, sample):
    """低于 EXPORT_VERSION 的缓存读时即作废；export_to_cache 触发重导，.i64 落 db_dir。"""
    runner, calls = fake_ida_runner()
    db_dir = tmp_path / "db"
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=runner, available=True, db_dir=db_dir)
    svc = DecompilerService(cache_dir=tmp_path / "cache", ida=ida)
    sha = sha256_file(str(sample))
    cache = tmp_path / "cache" / f"{sha}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    # v2 缓存对 v3 同样作废（契约升级读时即删重导）
    cache.write_text(json.dumps({"export_version": 2, "functions": []}), encoding="utf-8")

    assert svc.read_cached(sha) is None
    data, info = svc.export_to_cache(str(sample))
    assert data["export_version"] == 3 and len(data["strings"]) == 3
    assert [f["name"] for f in data["functions"]] == ["check_flag", "main"]
    assert calls["n"] == 1
    assert info == {"name": "ida-headless", "db_path": str(db_dir / f"{sha}.i64")}
    assert (db_dir / f"{sha}.i64").is_file()
    # 二次纯命中缓存，runner 不再跑
    svc.export_to_cache(str(sample))
    assert calls["n"] == 1


def test_export_without_backend_raises(tmp_path, sample):
    svc = DecompilerService(cache_dir=tmp_path / "cache")
    assert svc.headless_backends() == []
    with pytest.raises(RuntimeError):
        svc.export_to_cache(str(sample))


def test_factory_prefer_order(tmp_path):
    """工厂只装 headless 且按 prefer 排序；available=True 用假 runner，不探本机 PATH。"""
    runner = lambda args: (0, "", "")  # noqa: E731
    svc = build_headless_service(
        tmp_path / "c1", runner=runner, available=True,
        ida_db_dir=tmp_path / "db", ghidra_tmp_dir=tmp_path / "gt")
    assert [b.name for b in svc.headless_backends()] == ["ida-headless", "ghidra-headless"]
    svc2 = build_headless_service(
        tmp_path / "c2", runner=runner, prefer=("ghidra", "ida"), available=True,
        ida_db_dir=tmp_path / "db2", ghidra_tmp_dir=tmp_path / "gt2")
    assert [b.name for b in svc2.headless_backends()][0] == "ghidra-headless"


def test_build_xrefs_bidirectional_and_import_null():
    # main → check_flag（缓存内有地址）/ fgets（导入函数地址为 null）
    x = build_xrefs(EXPORT, 0x1234)
    assert x["name"] == "main" and x["address"] == "0x1234"
    callees = {c["name"]: c["address"] for c in x["callees"]}
    assert callees == {"check_flag": "0x1189", "fgets": None}
    # 反向：check_flag 的 caller 是 main
    back = build_xrefs(EXPORT, 0x1189)
    assert [c["name"] for c in back["callers"]] == ["main"]
    assert [c["address"] for c in back["callers"]] == ["0x1234"]
    # 未知函数
    assert build_xrefs(EXPORT, 0xDEAD) is None


def test_build_strings_hex_refs_join_and_filter():
    r = build_strings(EXPORT)
    assert r["truncated"] is False and len(r["items"]) == 3
    first = r["items"][0]
    assert first["address"] == "0x2000" and first["type"] == "cstr"
    # refs 附所属函数名（与 functions join）
    assert first["refs"] == [{"func": "0x1234", "func_name": "main", "from": "0x1240"}]
    # 多引用计数
    assert r["items"][1]["n_refs"] == 2
    # 无引用字符串 refs 为空
    assert r["items"][2]["refs"] == [] and r["items"][2]["type"] == "unicode"
    # q 大小写不敏感子串过滤
    hit = build_strings(EXPORT, "CoRrEcT")["items"]
    assert len(hit) == 1 and hit[0]["string"] == "Correct!"
    assert build_strings(EXPORT, "无此串")["items"] == []


def test_build_strings_truncation(monkeypatch):
    monkeypatch.setattr(dc, "STRINGS_LIMIT", 1)
    r = build_strings(EXPORT)
    assert r["truncated"] is True and len(r["items"]) == 1
    # q 过滤先于截断：只匹配 1 行时不报截断
    r2 = build_strings(EXPORT, "Correct!")
    assert r2["truncated"] is False and len(r2["items"]) == 1


def test_build_strings_missing_segment():
    # 旧/残缓存无 strings 段 → 空视图而非炸
    assert build_strings({"functions": []}) == {"items": [], "truncated": False}


# ---------- IDA 双向写回（apply_names / 锁检测 / 降级 / pull diff） ----------

def fake_apply_runner(fail_addr: int | None = None, calls=None):
    """假日 idat 写回：解析 -S"apply_names.py <in> <out>"，逐条回 results。"""
    if calls is None:
        calls = {"n": 0}

    def run(args):
        calls["n"] += 1
        calls["args"] = args
        sarg = next(a for a in args if a.startswith('-S"'))
        parts = sarg[3:].rstrip('"').split(" ")
        in_p, out_p = Path(parts[1]), Path(parts[2])
        payload = json.loads(in_p.read_text(encoding="utf-8"))
        calls["payload"] = payload
        results = []
        for it in payload["items"]:
            ea = int(it["address"])
            if fail_addr is not None and ea == fail_addr:
                results.append({"address": hex(ea), "ok": False, "error": "boom"})
            else:
                results.append({"address": hex(ea), "ok": True})
        out_p.write_text(json.dumps({"results": results}), encoding="utf-8")
        return 0, "ok", ""

    return run, calls


def _ida_with_db(tmp_path, sample, runner, sha=None):
    """建带 db_dir + 伪 .i64 的 IDA 服务，返回 (svc, db_dir, sha)。"""
    sha = sha or sha256_file(str(sample))
    db_dir = tmp_path / "db"
    db_dir.mkdir(parents=True)
    (db_dir / f"{sha}.i64").write_bytes(b"IDADB")
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=runner, available=True, db_dir=db_dir)
    svc = DecompilerService(cache_dir=tmp_path / "cache", ida=ida)
    return svc, db_dir, sha


def test_writeback_ok_args_and_temp_cleanup(tmp_path, sample):
    runner, calls = fake_apply_runner()
    svc, db_dir, sha = _ida_with_db(tmp_path, sample, runner)
    res = svc.writeback(sha, [
        {"address": 0x4011B6, "name": "check_flag", "comment": "XOR 0x5A 校验"},
    ])
    assert res["status"] == "ok" and res["applied"] == 1
    assert calls["n"] == 1
    # 末位参数是现有 .i64，无 -o（绝不重建库）；脚本是 apply_names.py
    args = calls["args"]
    assert args[-1] == str(db_dir / f"{sha}.i64")
    assert not any(a.startswith("-o") for a in args[1:])
    assert "apply_names.py" in " ".join(args)
    assert calls["payload"]["items"][0]["comment"] == "XOR 0x5A 校验"
    # in/out 临时件跑完即删，库不删
    assert list(db_dir.glob("_apply_*")) == []
    assert (db_dir / f"{sha}.i64").is_file()


def test_writeback_per_item_error_collected(tmp_path, sample):
    runner, _ = fake_apply_runner(fail_addr=0xDEAD)
    svc, _db, sha = _ida_with_db(tmp_path, sample, runner)
    res = svc.writeback(sha, [{"address": 0x4011B6, "name": "a"},
                              {"address": 0xDEAD, "name": "b"}])
    assert res["status"] == "ok" and res["applied"] == 1
    assert res["results"][1]["ok"] is False and res["results"][1]["error"] == "boom"


def test_writeback_locked_when_gui_open(tmp_path, sample):
    runner, calls = fake_apply_runner()
    svc, db_dir, sha = _ida_with_db(tmp_path, sample, runner)
    (db_dir / f"{sha}.id0").write_bytes(b"LOCK")  # GUI 锁文件
    res = svc.writeback(sha, [{"address": 1, "name": "x"}])
    assert res == {"status": "locked"} and calls["n"] == 0
    # 锁同样挡住库内重导（pull-names 前置）
    assert svc.refresh_db_cache(sha) == {"status": "locked"}


def test_writeback_states_no_db_no_tool_unsupported(tmp_path, sample):
    sha = sha256_file(str(sample))
    # no-db：db_dir 在但没有 .i64
    ida = IDAHeadlessBackend(runner=lambda a: (0, "", ""), available=True,
                             db_dir=tmp_path / "db2")
    svc = DecompilerService(cache_dir=tmp_path / "c1", ida=ida)
    assert svc.writeback(sha, [{"address": 1, "name": "x"}]) == {"status": "no-db"}
    assert svc.refresh_db_cache(sha) == {"status": "no-db"}
    # no-tool：什么后端都没有
    bare = DecompilerService(cache_dir=tmp_path / "c2")
    assert bare.writeback(sha, [{"address": 1, "name": "x"}])["status"] == "no-tool"
    assert bare.refresh_db_cache(sha)["status"] == "no-tool"
    # unsupported：只有 Ghidra（临时工程无持久写回语义）
    g = GhidraHeadlessBackend(runner=lambda a: (0, "", ""), available=True,
                              tmp_project_dir=tmp_path / "gt")
    only_g = DecompilerService(cache_dir=tmp_path / "c3", ghidra=g)
    assert only_g.writeback(sha, [{"address": 1, "name": "x"}])["status"] == "unsupported"
    assert only_g.refresh_db_cache(sha)["status"] == "unsupported"


# ---------- 大样本防崩（2026-09-29）：缓存驻留 / 轻量导入 / 列表截断 ----------

def test_read_cached_resident_and_mtime_invalidation(tmp_path, sample):
    """read_cached 单槽驻留（同 dict 对象复用，防大缓存逐请求 re-parse）；
    mtime 变化失效重读，删文件回 None。"""
    runner, _calls = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    data, _info = svc.export_to_cache(str(sample))
    sha = sha256_file(str(sample))
    first = svc.read_cached(sha)
    assert first == data
    again = svc.read_cached(sha)
    assert again is first  # 驻留命中：零 re-parse
    # mtime 变化 → 失效重读（新对象、新内容）
    cache_file = tmp_path / "cache" / f"{sha}.json"
    stale = cache_file.read_text(encoding="utf-8")
    cache_file.write_text(stale.replace("check_flag", "renamed_flag"),
                          encoding="utf-8")
    os.utime(cache_file, (time.time() + 10, time.time() + 10))
    fresh = svc.read_cached(sha)
    assert fresh is not again and fresh["functions"][0]["name"] == "renamed_flag"
    # 损坏缓存 → 删除并回 None，驻留同步清理
    cache_file.write_text("{broken", encoding="utf-8")
    os.utime(cache_file, (time.time() + 20, time.time() + 20))
    assert svc.read_cached(sha) is None and not cache_file.exists()
    assert svc.read_cached(sha) is None  # 文件已不在
    assert svc.read_cached("b" * 64) is None


def test_import_ida_mcp_cache_lightweight(tmp_path):
    """GUI IDA 拉取的轻量缓存落盘：v3 契约兼容、无伪码/strings/sections/imports
    （数据大头不落平台盘，点查走 MCP 实时降级）。"""
    svc = DecompilerService(cache_dir=tmp_path / "cache")
    funcs = [{"address": 0x1189, "name": "check_flag", "size": 96}]
    data = svc.import_ida_mcp_cache("a" * 64, funcs, binary_name="x.elf")
    assert data["export_version"] == dc.EXPORT_VERSION
    assert data["meta"]["source"] == "ida-mcp" and data["binary"] == "x.elf"
    assert data["functions"] == funcs
    assert data["strings"] == [] and data["sections"] == [] and data["imports"] == {}
    # 落盘可回读（导入后驻留已失效，read_cached 从盘上重 parse）
    back = svc.read_cached("a" * 64)
    assert back["functions"] == funcs and back["meta"]["source"] == "ida-mcp"
    assert (tmp_path / "cache" / f"{'a' * 64}.json").is_file()
    # partial 落盘（断点续拉）：partial/total_functions/next_offset 进 meta；
    # 完成态（partial=False）不写这些字段
    svc.import_ida_mcp_cache("c" * 64, funcs, partial=True, total=99, next_offset=100)
    part = svc.read_cached("c" * 64)
    assert part["meta"]["partial"] is True and part["meta"]["total_functions"] == 99
    assert part["meta"]["next_offset"] == 100
    assert "partial" not in svc.read_cached("a" * 64)["meta"]


def test_func_detail_cache_roundtrip_and_truncation(tmp_path):
    """按需详情缓存（每函数一文件）：读写回环 + 硬上限截断（伪码 512K/反汇编 5000 行）
    + 缺失/损坏回 None 并删除坏文件。"""
    svc = DecompilerService(cache_dir=tmp_path / "cache")
    sha, addr = "a" * 64, 0x401000
    detail = {"address": hex(addr), "name": "win_main", "size": 200,
              "pseudocode": "int x;" * 200000,          # > 512K 字符
              "disasm_lines": [f"insn {i}" for i in range(6000)],  # > 5000 行
              "callers": [{"address": "0x400000", "name": "caller"}],
              "callees": []}
    svc.save_func_detail(sha, addr, detail)
    f = svc._detail_file(sha, addr)
    assert f.is_file()
    back = svc.read_func_detail(sha, addr)
    assert back is not None and back["name"] == "win_main"
    assert len(back["pseudocode"]) <= dc.DETAIL_PSEUDO_MAX_CHARS
    assert back.get("pseudocode_truncated") is True
    assert len(back["disasm_lines"]) <= dc.DETAIL_DISASM_MAX_LINES
    assert back.get("disasm_truncated") is True
    assert f.stat().st_size <= dc.DETAIL_FILE_MAX_BYTES
    # 缺失 → None
    assert svc.read_func_detail(sha, 0x999999) is None
    # 损坏 → 删除并回 None（下次自动重拉）
    f.write_text("{broken", encoding="utf-8")
    assert svc.read_func_detail(sha, addr) is None and not f.exists()


def test_mcp_func_detail_parses_analyze_batch(tmp_path):
    """analyze_batch 单查询 → func_detail 解析：伪码/反汇编行/callers/callees；
    请求参数关闭非必要段 + 带上限（40万样本按需详情防大响应）。"""
    def route(args, t):
        q = args["queries"][0]
        assert q["include_strings"] is False and q["include_constants"] is False
        assert q["include_basic_blocks"] is False and q["include_proto"] is False
        assert q["max_disasm_insns"] == dc.DETAIL_DISASM_MAX_LINES
        return [{
            "query": q["query"], "addr": "0x401234", "name": "win_main",
            "analysis": {
                "size": "0xc8",
                "decompile": "int win_main() { return 0; }",
                "disasm": {"lines": ["push rbp", "ret"], "instruction_count": 2,
                           "truncated": False},
                "callers": [{"addr": "0x401000", "name": "entry"}],
                "callees": [{"addr": "0x401500", "name": "helper"}],
            },
            "error": None,
        }]
    mcp = MCPBackend(transport=FakeMCPTransport(routes={"analyze_batch": route}))
    d = mcp.func_detail(0x401234)
    assert d is not None
    assert d["pseudocode"] == "int win_main() { return 0; }"
    assert d["disasm_lines"] == ["push rbp", "ret"] and d["disasm_truncated"] is False
    assert d["callers"] == [{"address": "0x401000", "name": "entry"}]
    assert d["callees"] == [{"address": "0x401500", "name": "helper"}]
    assert d["size"] == 0xC8
    # 工具缺失（tools/list 无 analyze_batch）→ None（不抛）
    mcp2 = MCPBackend(transport=FakeMCPTransport(
        tools=["decompile", "list_funcs"]))
    assert mcp2.func_detail(0x401234) is None


def test_ensure_func_detail_pulls_persists_and_offline(tmp_path, sample):
    """按需详情主通道：首次 MCP 拉取+落盘；二次命中缓存不再调 MCP；
    离线未缓存 None；离线已缓存仍可读（渐进累积的离线可用性）。"""
    def route(args, t):
        q = args["queries"][0]
        if q["query"] != "0x401234":
            return [{"query": q["query"], "addr": None, "name": None,
                     "analysis": None, "error": "not found"}]
        return [{"query": q["query"], "addr": "0x401234", "name": "win_main",
                 "analysis": {"size": "0xc8", "decompile": "code",
                              "disasm": {"lines": ["nop"], "truncated": False},
                              "callers": [], "callees": []},
                 "error": None}]
    transport = FakeMCPTransport(routes={"analyze_batch": route})
    svc, mcp = _mcp_service(tmp_path, transport)
    sha = "a" * 64
    d1 = svc.ensure_func_detail(sha, 0x401234)
    assert d1 is not None and d1["pseudocode"] == "code"
    assert d1["disasm_lines"] == ["nop"]
    assert svc._detail_file(sha, 0x401234).is_file()
    n = len(transport.calls)
    d2 = svc.ensure_func_detail(sha, 0x401234)
    assert d2 == d1
    assert len(transport.calls) == n  # 详情文件命中，无第二次 analyze_batch
    # 未缓存 + MCP 离线 → None
    mcp._transport = _dead_transport
    mcp._health = None
    assert svc.ensure_func_detail(sha, 0x409999) is None
    # 离线但详情已缓存 → 命中（离线可用）
    assert svc.ensure_func_detail(sha, 0x401234)["pseudocode"] == "code"


def test_list_functions_truncates_large_result(tmp_path, sample):
    """数万函数全量 dump 淹没上下文——>500 行截断并提示用过滤参数（大样本防崩）。"""
    runner, _calls = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    sha = sha256_file(str(sample))
    big = {"export_version": dc.EXPORT_VERSION, "binary": "big.elf",
           "meta": {}, "sections": [], "imports": {},
           "functions": [{"address": 0x1000 + i, "name": f"fn_{i:05d}",
                          "size": 16, "calls": [], "pseudocode": "x"}
                         for i in range(600)],
           "strings": []}
    (tmp_path / "cache" / f"{sha}.json").write_text(json.dumps(big),
                                                   encoding="utf-8")
    text = svc.list_functions(str(sample))
    assert "已截断" in text and "name_contains" in text
    rows = json.loads(text.split("\n…")[0])
    assert len(rows) == 500
    # 过滤后不触顶则无截断（fn_00010 不含 fn_00001 子串）
    hit = json.loads(svc.list_functions(str(sample), name_contains="fn_00001"))
    assert len(hit) == 1 and hit[0]["name"] == "fn_00001"


def test_refresh_db_cache_exports_without_rebuild(tmp_path, sample):
    """pull 前置：对现有 .i64 重导（无 -o、不删库），缓存被 GUI 当前名覆盖。"""
    fresh = {**EXPORT, "functions": [
        {"address": 0x1189, "name": "check_flag", "size": 96, "calls": [],
         "pseudocode": "x"},
        {"address": 0x1234, "name": "win_main", "size": 10, "calls": [], "pseudocode": ""},
    ]}
    captured = {}

    def run(args):
        captured["args"] = args
        sarg = next(a for a in args if a.startswith('-S"'))
        out_p = Path(sarg[3:].rstrip('"').split(" ", 1)[1])
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(fresh), encoding="utf-8")
        return 0, "", ""

    svc, db_dir, sha = _ida_with_db(tmp_path, sample, run)
    res = svc.refresh_db_cache(sha)
    assert res["status"] == "ok"
    assert [f["name"] for f in res["data"]["functions"]] == ["check_flag", "win_main"]
    args = captured["args"]
    assert not any(a.startswith("-o") for a in args[1:]) and args[-1].endswith(".i64")
    assert (db_dir / f"{sha}.i64").read_bytes() == b"IDADB"  # 库没被删/重建
    assert json.loads((tmp_path / "cache" / f"{sha}.json").read_text(
        encoding="utf-8"))["functions"][1]["name"] == "win_main"


def test_diff_pulled_names_filters_auto_and_same():
    data = {"functions": [
        {"address": 0x1000, "name": "sub_1000"},
        {"address": 0x1010, "name": "win_main"},
        {"address": 0x1020, "name": "same_name"},
        {"address": 0x1030, "name": "nullsub_3"},
        {"address": 0x1040, "name": "unk_2040"},
    ]}
    kb = [
        {"id": "func-1", "address": 0x1000, "name": "my_helper"},   # 新名是自动名 → 不拉
        {"id": "func-2", "address": 0x1010, "name": "old_main"},    # 真人改名 → 拉
        {"id": "func-3", "address": 0x1020, "name": "same_name"},   # 同名不动
        {"id": "func-4", "address": 0x1030, "name": "early_guess"}, # nullsub → 不拉
        {"id": "func-5", "address": 0x1050, "name": "lonely"},      # 缓存缺该函数 → 不拉
    ]
    changed = diff_pulled_names(data, kb)
    assert changed == [{"func_id": "func-2", "address": "0x1010",
                        "old_name": "old_main", "new_name": "win_main"}]


def test_is_auto_name():
    assert is_auto_name("sub_4011b6") and is_auto_name("nullsub") and is_auto_name("unk_3")
    assert not is_auto_name("check_flag") and not is_auto_name("my_sub_4011b6")
    assert is_auto_name(None) and is_auto_name("")


# ---------- 大样本加速 P1（2026-09-30）：可配超时 / 直读存量 / 全局 sha 缓存 / 导入 ----------

def test_resolve_headless_timeout_config_override(tmp_path, monkeypatch):
    """headless 超时：默认放开数小时；config/decompiler.json 的 headless_timeout 覆盖，
    非法/缺失/损坏回默认（大样本不轻易腰斩分析）。"""
    cfg = tmp_path / "decompiler.json"
    monkeypatch.setattr(dc, "DECOMPILER_CONFIG_PATH", cfg)
    assert dc.resolve_headless_timeout() == float(dc.HEADLESS_TIMEOUT)  # 无配置
    cfg.write_text(json.dumps({"headless_timeout": 7200}), encoding="utf-8")
    assert dc.resolve_headless_timeout() == 7200.0                      # 覆盖生效
    cfg.write_text(json.dumps({"headless_timeout": 0}), encoding="utf-8")
    assert dc.resolve_headless_timeout() == float(dc.HEADLESS_TIMEOUT)  # 0 忽略
    cfg.write_text(json.dumps({"headless_timeout": "abc"}), encoding="utf-8")
    assert dc.resolve_headless_timeout() == float(dc.HEADLESS_TIMEOUT)  # 非数忽略
    cfg.write_text("{broken", encoding="utf-8")
    assert dc.resolve_headless_timeout() == float(dc.HEADLESS_TIMEOUT)  # 损坏忽略


def test_default_runner_passes_configured_timeout(tmp_path, monkeypatch):
    """_default_runner 每次现取超时（改配置无需重启）：透传到 subprocess.run。"""
    cfg = tmp_path / "decompiler.json"
    cfg.write_text(json.dumps({"headless_timeout": 1234}), encoding="utf-8")
    monkeypatch.setattr(dc, "DECOMPILER_CONFIG_PATH", cfg)
    captured: dict = {}

    class _P:
        returncode, stdout, stderr = 0, "o", "e"

    def fake_run(args, **kw):
        captured["args"] = args
        captured.update(kw)
        return _P()

    monkeypatch.setattr(dc.subprocess, "run", fake_run)
    rc, out, err = dc._default_runner(["idat", "-A"])
    assert (rc, out, err) == (0, "o", "e")
    assert captured["timeout"] == 1234.0 and captured["capture_output"] is True


def _export_db_runner(write=None, calls=None):
    """假日 idat：库内重导（无 -o，末位是库）——抠 -S"<script> <out>" 写 canned JSON。"""
    if write is None:
        write = EXPORT
    if calls is None:
        calls = {"n": 0}

    def run(args):
        calls["n"] += 1
        calls["args"] = args
        sarg = next(a for a in args if a.startswith('-S"'))
        out_p = Path(sarg[3:].rstrip('"').split(" ", 1)[1])
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(write), encoding="utf-8")
        return 0, "idat ok", ""

    return run, calls


def test_export_reuses_existing_db_without_rebuild(tmp_path, sample):
    """直读存量：库已存在 → 库内重导（无 -o、不删库）并标记 reused；GUI 占用则拒绝。"""
    sha = sha256_file(str(sample))
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    (db_dir / f"{sha}.i64").write_bytes(b"IDADB")
    runner, calls = _export_db_runner()
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=runner, available=True, db_dir=db_dir)
    info = ida.export(str(sample), tmp_path / "out.json")
    assert info["reused"] is True and info["db_path"] == str(db_dir / f"{sha}.i64")
    assert not any(a.startswith("-o") for a in calls["args"][1:])  # 没有 -o 重建
    assert calls["args"][-1] == str(db_dir / f"{sha}.i64")
    assert (db_dir / f"{sha}.i64").read_bytes() == b"IDADB"       # 库原封不动
    # 库被 GUI 占用（锁文件）→ 绝不重导
    (db_dir / f"{sha}.id0").write_bytes(b"LOCK")
    with pytest.raises(RuntimeError, match="ida-db-locked"):
        ida.export(str(sample), tmp_path / "out2.json")


def test_global_cache_publish_and_promote_across_projects(tmp_path, sample):
    """全量导出落全局 sha 缓存；另一项目本地空时从全局提升（免重跑 headless）。"""
    sha = sha256_file(str(sample))
    global_dir = tmp_path / "global"
    runner_a, calls_a = fake_ida_runner()
    ida_a = IDAHeadlessBackend(idat_cmd="idat", runner=runner_a, available=True,
                               db_dir=tmp_path / "dbA")
    svc_a = DecompilerService(cache_dir=tmp_path / "cacheA", ida=ida_a,
                              global_cache_dir=global_dir)
    data_a, _info = svc_a.export_to_cache(str(sample))
    assert data_a["export_version"] == dc.EXPORT_VERSION and calls_a["n"] == 1
    assert (global_dir / f"{sha}.json").is_file()                 # 已发布全局
    # 项目 B：本地缓存空 → read_cached 触发 promote（不跑任何后端）
    runner_b, calls_b = fake_ida_runner()
    ida_b = IDAHeadlessBackend(idat_cmd="idat", runner=runner_b, available=True,
                               db_dir=tmp_path / "dbB")
    svc_b = DecompilerService(cache_dir=tmp_path / "cacheB", ida=ida_b,
                              global_cache_dir=global_dir)
    promoted = svc_b.read_cached(sha)
    assert promoted is not None and promoted["functions"][0]["name"] == "check_flag"
    assert calls_b["n"] == 0                                      # 零 headless
    assert (tmp_path / "cacheB" / f"{sha}.json").is_file()        # 已拷回本地
    # export_to_cache 亦命中全局（本地已 promote 命中）
    data_b, _ = svc_b.export_to_cache(str(sample))
    assert calls_b["n"] == 0 and data_b["export_version"] == dc.EXPORT_VERSION
    # 未配 global_cache_dir 时 promote/publish 皆为 no-op（不回 None 崩）
    bare = DecompilerService(cache_dir=tmp_path / "cacheC")
    assert bare.read_cached(sha) is None and bare.global_cache_dir is None


def test_import_export_json_accepts_v3_and_rejects_low(tmp_path):
    """导入外部全量导出 JSON：>=v3 落盘并发布会全局；低契约/非对象结构化拒绝、不落盘。"""
    sha = "d" * 64
    global_dir = tmp_path / "global"
    svc = DecompilerService(cache_dir=tmp_path / "cache", global_cache_dir=global_dir)
    src = tmp_path / "export.json"
    src.write_text(json.dumps(EXPORT), encoding="utf-8")
    assert svc.import_export_json(sha, src) == {
        "status": "ok", "functions": 2, "export_version": dc.EXPORT_VERSION}
    assert svc.read_cached(sha)["functions"][0]["name"] == "check_flag"
    assert (global_dir / f"{sha}.json").is_file()
    # 低契约拒绝（不落盘）
    low = tmp_path / "low.json"
    low.write_text(json.dumps({"export_version": 2, "functions": []}), encoding="utf-8")
    res = svc.import_export_json("e" * 64, low)
    assert res["status"] == "error" and "版本过低" in res["reason"]
    assert not (tmp_path / "cache" / f"{'e' * 64}.json").exists()
    # 顶层非对象 / 损坏
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    assert svc.import_export_json("f" * 64, bad)["status"] == "error"
    assert svc.import_export_json("g" * 64, tmp_path / "nope.json")["status"] == "error"


def test_import_ida_db_copies_reexports_and_publishes(tmp_path, sample):
    """纳入外部 IDA 库：拷进项目 db_dir → 库内重导为缓存 → 落全局；免 MCP 回拉一轮。"""
    sha = sha256_file(str(sample))
    db_dir = tmp_path / "db"
    global_dir = tmp_path / "global"
    ext_db = tmp_path / "incoming" / "sample.i64"
    ext_db.parent.mkdir(parents=True)
    ext_db.write_bytes(b"EXT-IDB")
    runner, calls = _export_db_runner()
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=runner, available=True, db_dir=db_dir)
    svc = DecompilerService(cache_dir=tmp_path / "cache", ida=ida,
                            global_cache_dir=global_dir)
    res = svc.import_ida_db(sha, ext_db)
    assert res["status"] == "ok" and res["functions"] == 2
    assert res["db_path"] == str(db_dir / f"{sha}.i64")
    assert (db_dir / f"{sha}.i64").read_bytes() == b"EXT-IDB"     # 拷进项目库
    assert calls["args"][-1] == str(db_dir / f"{sha}.i64")        # 库内重导
    assert not any(a.startswith("-o") for a in calls["args"][1:])
    assert svc.read_cached(sha)["functions"][0]["name"] == "check_flag"
    assert (global_dir / f"{sha}.json").is_file()                 # 已发布全局
    # 非 IDA 库扩展名 → 结构化错误，不落库
    bad = tmp_path / "x.bin"
    bad.write_bytes(b"nope")
    assert svc.import_ida_db("b" * 64, bad)["status"] == "error"
    assert not (db_dir / f"{'b' * 64}.i64").exists()


def test_import_ida_db_locked_and_no_tool(tmp_path, sample):
    """目标库正被 GUI 占用 → locked 绝不覆盖；无 IDA 后端 → no-tool/unsupported。"""
    sha = sha256_file(str(sample))
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    (db_dir / f"{sha}.i64").write_bytes(b"OLD")
    (db_dir / f"{sha}.id0").write_bytes(b"LOCK")
    src = tmp_path / "new.i64"
    src.write_bytes(b"NEW")
    runner, _calls = _export_db_runner()
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=runner, available=True, db_dir=db_dir)
    svc = DecompilerService(cache_dir=tmp_path / "cache", ida=ida)
    assert svc.import_ida_db(sha, src) == {"status": "locked"}
    assert (db_dir / f"{sha}.i64").read_bytes() == b"OLD"
    # 无任何后端
    bare = DecompilerService(cache_dir=tmp_path / "c2")
    assert bare.import_ida_db(sha, src)["status"] == "no-tool"
    # 仅 Ghidra（无持久库语义）
    g = GhidraHeadlessBackend(runner=lambda a: (0, "", ""), available=True,
                              tmp_project_dir=tmp_path / "gt")
    only_g = DecompilerService(cache_dir=tmp_path / "c3", ghidra=g)
    assert only_g.import_ida_db(sha, src)["status"] == "unsupported"


def test_factory_threads_global_cache_dir(tmp_path):
    """工厂把 global_cache_dir 透传到 DecompilerService（跨项目复用落点）。"""
    svc = build_headless_service(
        tmp_path / "c", runner=lambda a: (0, "", ""), available=True,
        ida_db_dir=tmp_path / "db", ghidra_tmp_dir=tmp_path / "gt",
        global_cache_dir=tmp_path / "global")
    assert svc.global_cache_dir == tmp_path / "global"


# ---------- 大样本加速 P2（2026-09-30）：Ghidra 多进程并行分片主产 ----------

def test_resolve_ghidra_workers_default_clamp_and_override(tmp_path, monkeypatch):
    """worker 数：默认 max(1, CPU-1)；config 覆盖；一律夹 [1, GHIDRA_MAX_WORKERS]（内存保护）。"""
    cfg = tmp_path / "decompiler.json"
    monkeypatch.setattr(dc, "DECOMPILER_CONFIG_PATH", cfg)
    monkeypatch.setattr(dc.os, "cpu_count", lambda: 8)
    assert dc.resolve_ghidra_workers() == 7                        # 默认 CPU-1
    cfg.write_text(json.dumps({"ghidra_workers": 3}), encoding="utf-8")
    assert dc.resolve_ghidra_workers() == 3                        # 覆盖生效
    cfg.write_text(json.dumps({"ghidra_workers": 999}), encoding="utf-8")
    assert dc.resolve_ghidra_workers() == dc.GHIDRA_MAX_WORKERS    # 夹上限（内存保护）
    cfg.write_text(json.dumps({"ghidra_workers": 0}), encoding="utf-8")
    assert dc.resolve_ghidra_workers() == 7                        # ≤0 忽略回默认
    cfg.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(dc.os, "cpu_count", lambda: 1)
    assert dc.resolve_ghidra_workers() == 1                        # CPU=1 → max(1, 0)


def test_resolve_large_sample_bytes_and_is_large_sample(tmp_path, monkeypatch):
    """大样本阈值可覆盖；is_large_sample 按文件大小判定，缺失文件一律非大样本不抛。"""
    cfg = tmp_path / "decompiler.json"
    monkeypatch.setattr(dc, "DECOMPILER_CONFIG_PATH", cfg)
    assert dc.resolve_large_sample_bytes() == dc.LARGE_SAMPLE_BYTES
    cfg.write_text(json.dumps({"large_sample_bytes": 100}), encoding="utf-8")
    assert dc.resolve_large_sample_bytes() == 100
    small = tmp_path / "s.bin"
    small.write_bytes(b"x" * 50)
    big = tmp_path / "b.bin"
    big.write_bytes(b"x" * 200)
    assert dc.is_large_sample(str(big)) and not dc.is_large_sample(str(small))
    assert dc.is_large_sample(str(tmp_path / "missing.bin")) is False


def _tagged(inner, tag, order):
    def run(args):
        order.append(tag)
        return inner(args)
    return run


def test_large_sample_routes_ghidra_first(tmp_path, monkeypatch):
    """大样本（≥阈值）Ghidra 并行主产优先；普通样本维持装配顺序（IDA 优先）。"""
    order: list = []
    g_runner, _ = fake_ghidra_runner(tmp_path)
    i_runner, _ = fake_ida_runner()
    g = GhidraHeadlessBackend(runner=_tagged(g_runner, "ghidra", order),
                              available=True, tmp_project_dir=tmp_path / "gt")
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=_tagged(i_runner, "ida", order),
                             available=True, db_dir=tmp_path / "db")
    svc = DecompilerService(cache_dir=tmp_path / "cache")
    svc.backends = [ida, g]        # 模拟工厂 prefer=("ida","ghidra") 装配顺序
    small = tmp_path / "small.elf"
    small.write_bytes(b"MZ" + b"\x00" * 16)
    svc.export_to_cache(str(small))
    assert order == ["ida"]        # 普通样本 IDA 优先
    monkeypatch.setattr(dc, "LARGE_SAMPLE_BYTES", 4)   # 免造 20MB 真文件
    order.clear()
    big = tmp_path / "big.elf"
    big.write_bytes(b"MZ" + b"\x00" * 15 + b"\x01")   # 内容异于 small，避免 sha 缓存互撞
    svc.export_to_cache(str(big))
    assert order == ["ghidra"]     # 大样本 Ghidra 优先


def test_engine_normalize_and_backend_mapping():
    """引擎名规整与后端名→引擎映射（2026-10-01 双模式）。"""
    assert dc.normalize_engine(None) == "ida"
    assert dc.normalize_engine("") == "ida"
    assert dc.normalize_engine("IDA") == "ida"
    assert dc.normalize_engine("ghidra") == "ghidra"
    assert dc.normalize_engine("Ghidra") == "ghidra"
    assert dc.normalize_engine("乱写") == "ida"
    assert dc.engine_of_backend("ida-headless") == "ida"
    assert dc.engine_of_backend("mcp") == "ida"
    assert dc.engine_of_backend("ghidra-headless") == "ghidra"


def test_engine_preference_reorders_backends(tmp_path, sample):
    """engine 指定时该引擎后端提到最前（偏好非排他，另一引擎仍兜底）。"""
    g_runner, _ = fake_ghidra_runner(tmp_path)
    i_runner, _ = fake_ida_runner()
    g = GhidraHeadlessBackend(runner=g_runner, available=True,
                              tmp_project_dir=tmp_path / "gt")
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=i_runner, available=True,
                             db_dir=tmp_path / "db")
    svc = DecompilerService(cache_dir=tmp_path / "cache")
    svc.backends = [ida, g]
    assert [b.name for b in svc._ordered_export_backends(str(sample))] \
        == ["ida-headless", "ghidra-headless"]
    assert [b.name for b in svc._ordered_export_backends(str(sample), "ghidra")] \
        == ["ghidra-headless", "ida-headless"]
    assert [b.name for b in svc._ordered_export_backends(str(sample), "ida")] \
        == ["ida-headless", "ghidra-headless"]


def test_export_marks_engine_in_cache_meta(tmp_path, sample):
    """全量导出在缓存 meta 写产出引擎（Ghidra 后端产出 → meta.engine=ghidra）。"""
    runner, _ = fake_ghidra_runner(tmp_path)
    svc = make_service(tmp_path, sample, runner)
    data, _info = svc.export_to_cache(str(sample), engine="ghidra")
    assert data["meta"]["engine"] == "ghidra"
    # 落盘缓存也带该字段（供 overview 读）
    assert svc.read_cached(sha256_file(str(sample)))["meta"]["engine"] == "ghidra"


def test_ghidra_export_embeds_disasm_for_small_sample(tmp_path, sample):
    """Ghidra 模式 + 小样本：导出脚本带 args[4]='1'（内嵌反汇编开关）。"""
    captured: dict = {}

    def run(args):
        captured["args"] = args
        out = Path(args[args.index("-postScript") + 2])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(EXPORT), encoding="utf-8")
        return 0, "ok", ""

    g = GhidraHeadlessBackend(runner=run, available=True,
                              tmp_project_dir=tmp_path / "gt")
    svc = DecompilerService(cache_dir=tmp_path / "cache", ghidra=g)
    svc.export_to_cache(str(sample), engine="ghidra")
    args = captured["args"]
    # 反汇编开关位于 -deleteProject 之前，值为 "1"
    assert args[args.index("-deleteProject") - 1] == "1"
    # 非 Ghidra 引擎（ida）时不带该开关
    captured.clear()
    svc2 = DecompilerService(cache_dir=tmp_path / "c2", ghidra=GhidraHeadlessBackend(
        runner=run, available=True, tmp_project_dir=tmp_path / "gt2"))
    svc2.export_to_cache(str(sample), engine="ida")
    assert captured["args"][captured["args"].index("-deleteProject") - 1] != "1"


def test_ghidra_disasm_on_demand_reuses_persistent_project(tmp_path, sample):
    """大样本按需反汇编：首次 -import（建持久工程），之后 -process -noanalysis 复用；
    解析 disasm_funcs.py 的 {hex_addr: [lines]} 输出。"""
    calls: list = []

    def run(args):
        calls.append(list(args))
        out = Path(args[args.index("-postScript") + 2])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"0x1189": ["0x1189  MOV EAX,1", "0x118b  RET"]}),
                       encoding="utf-8")
        # 模拟工程落盘（.gpr）→ 下次走 -process
        proj_dir = Path(args[1])
        (proj_dir / "csproj.gpr").write_text("x", encoding="utf-8")
        return 0, "ok", ""

    g = GhidraHeadlessBackend(runner=run, available=True,
                              tmp_project_dir=tmp_path / "gt")
    got = g.disasm_on_demand(str(sample), [0x1189], max_lines=400)
    assert got == {0x1189: ["0x1189  MOV EAX,1", "0x118b  RET"]}
    assert "-import" in calls[0] and "-process" not in calls[0]
    # 第二次：既有工程 → -process -noanalysis（免重导入）
    g.disasm_on_demand(str(sample), [0x1189])
    assert "-process" in calls[1] and "-noanalysis" in calls[1]
    assert "-import" not in calls[1]


def test_ensure_func_detail_ghidra_uses_cache_and_disasm(tmp_path, sample):
    """Ghidra 模式按需详情：伪码/调用关系取缓存；反汇编取缓存内嵌或按需生成。"""
    with_disasm = copy.deepcopy(EXPORT)
    with_disasm["functions"][0]["disasm_lines"] = ["0x1189  push rbp"]
    runner = lambda a: (0, "", "")
    g = GhidraHeadlessBackend(runner=runner, available=True,
                              tmp_project_dir=tmp_path / "gt")
    svc = DecompilerService(cache_dir=tmp_path / "cache", ghidra=g)
    sha = sha256_file(str(sample))
    svc.cache_dir.mkdir(parents=True, exist_ok=True)
    svc._write_cache_file(svc._cache_file(sha), with_disasm)
    detail = svc.ensure_func_detail(sha, 0x1189, engine="ghidra", binary=str(sample))
    assert detail is not None and detail["engine"] == "ghidra"
    assert detail["pseudocode"].startswith("int check_flag")
    assert detail["disasm_lines"] == ["0x1189  push rbp"]
    assert detail["callers"] == [{"address": "0x1234", "name": "main"}]
    assert svc._detail_file(sha, 0x1189).is_file()  # 命中伪码/反汇编 → 落盘


def test_ghidra_export_passes_workers_when_gt1(tmp_path, sample):
    """postScript 固定 5 位实参 [workers, progress, stop, analyze, want_disasm]；
    -noanalysis 跳过内置分析（自驱分析开关恒 "1"）。"""
    captured: dict = {}

    def run(args):
        captured["args"] = args
        out = Path(args[args.index("-postScript") + 2])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(EXPORT), encoding="utf-8")
        return 0, "ok", ""

    g = GhidraHeadlessBackend(runner=run, available=True, workers=4,
                              tmp_project_dir=tmp_path / "gt")
    g.export(str(sample), tmp_path / "o.json")
    args = captured["args"]
    i = args.index("-postScript")
    assert args[i + 3] == "4"            # workers
    assert "-noanalysis" in args          # 自驱分析：跳过内置分析
    assert args[i + 6] == "1"            # 自驱分析开关
    captured.clear()
    g1 = GhidraHeadlessBackend(runner=run, available=True, workers=1,
                               tmp_project_dir=tmp_path / "gt")
    g1.export(str(sample), tmp_path / "o1.json")
    args1 = captured["args"]
    i1 = args1.index("-postScript")
    assert args1[i1 + 3:i1 + 8] == ["1", "", "", "1", ""]  # 无 ctl/无内嵌填空串


def test_export_marks_stoppable_by_routed_backend(tmp_path, sample):
    """progress.stoppable 由实际选路后端决定（Ghidra 可协作停 → True；IDA → False）。"""
    g_runner, _ = fake_ghidra_runner(tmp_path)
    i_runner, _ = fake_ida_runner()
    g = GhidraHeadlessBackend(runner=g_runner, available=True,
                              tmp_project_dir=tmp_path / "gt")
    ida = IDAHeadlessBackend(idat_cmd="idat", runner=i_runner, available=True,
                             db_dir=tmp_path / "db")
    svc = DecompilerService(cache_dir=tmp_path / "cache")
    svc.backends = [ida, g]
    prog: dict = {}
    svc.export_to_cache(str(sample), progress=prog, engine="ghidra")
    assert prog.get("stoppable") is True
    svc2 = DecompilerService(cache_dir=tmp_path / "c2")
    svc2.backends = [ida, g]
    prog2: dict = {}
    svc2.export_to_cache(str(sample), progress=prog2, engine="ida")
    assert prog2.get("stoppable") is False


def test_factory_threads_ghidra_workers(tmp_path, monkeypatch):
    """工厂把 resolve_ghidra_workers() 透传给 Ghidra 后端（并行分片落点）。"""
    monkeypatch.setattr(dc, "resolve_ghidra_workers", lambda: 5)
    svc = build_headless_service(
        tmp_path / "c", runner=lambda a: (0, "", ""), prefer=("ghidra",),
        available=True, ghidra_tmp_dir=tmp_path / "gt")
    assert svc.headless_backends()[0].workers == 5



# ---------- 大样本加速 P3（2026-09-30）：协作式停止 + 分片进度 + partial ----------

def _ghidra_ctl_runner(write=None, calls=None, wait_stop=2.0):
    """假 analyzeHeadless（带控制文件）：写一帧进度；等停止文件最多 wait_stop 秒，
    出现则产出 partial（仅函数名/地址、无伪码），否则整份 canned v3。"""
    if write is None:
        write = EXPORT
    if calls is None:
        calls = {"n": 0}

    def run(args):
        calls["n"] += 1
        calls["args"] = args
        i = args.index("-postScript")
        out = Path(args[i + 2])
        prog = Path(args[i + 4]) if len(args) > i + 4 else None
        stop = Path(args[i + 5]) if len(args) > i + 5 else None
        out.parent.mkdir(parents=True, exist_ok=True)
        if prog is not None:
            prog.write_text(json.dumps({"done": 1, "total": 2, "phase": "decompile"}),
                            encoding="utf-8")
        stopped = False
        if stop is not None:
            deadline = time.time() + wait_stop
            while not stop.is_file() and time.time() < deadline:
                time.sleep(0.02)
            stopped = stop.is_file()
        data = json.loads(json.dumps(write))
        if stopped:
            data["partial"] = True
            data["stopped"] = True
            data["meta"]["partial"] = True
            data["functions"] = [{"address": f["address"], "name": f["name"],
                                  "size": f["size"], "calls": []}
                                 for f in data.get("functions", [])]
        out.write_text(json.dumps(data), encoding="utf-8")
        return 0, "ok", ""

    return run, calls


def test_is_partial_export_detection():
    assert is_partial_export({"meta": {"partial": True}}) is True
    assert is_partial_export({"partial": True}) is True
    assert is_partial_export({"stopped": True}) is True
    assert is_partial_export({"meta": {"partial": False}}) is False
    assert is_partial_export(EXPORT) is False
    assert is_partial_export(None) is False


def test_ghidra_export_passes_controls_when_requested(tmp_path, sample):
    """带 progress/stop_event 时把 worker 数 + 进度文件 + 停止文件作为 postScript
    位置实参传给脚本；监视线程回填进度；控制文件跑完即清。"""
    runner, calls = _ghidra_ctl_runner()
    g = GhidraHeadlessBackend(runner=runner, available=True, workers=3,
                              tmp_project_dir=tmp_path / "gt")
    prog: dict = {}
    info = g.export(str(sample), tmp_path / "o.json", progress=prog)
    args = calls["args"]
    i = args.index("-postScript")
    assert args[i + 3] == "3"                       # workers
    assert args[i + 4].endswith(".json.progress")   # 进度文件
    assert args[i + 5].endswith(".json.stop")       # 停止文件
    assert prog["done"] == 1 and prog["total"] == 2  # 末次进度回填
    assert info["stopped"] is False
    assert not Path(args[i + 4]).exists() and not Path(args[i + 5]).exists()  # 已清理


def test_cooperative_stop_marks_partial_and_skips_global(tmp_path, sample):
    """协作式停止：stop_event 已置位 → 脚本产出 partial；partial 不发布全局。"""
    sha = sha256_file(str(sample))
    global_dir = tmp_path / "global"
    runner, _calls = _ghidra_ctl_runner()
    g = GhidraHeadlessBackend(runner=runner, available=True,
                              tmp_project_dir=tmp_path / "gt")
    svc = DecompilerService(cache_dir=tmp_path / "cache", ghidra=g,
                            global_cache_dir=global_dir)
    stop = threading.Event()
    stop.set()
    data, info = svc.export_to_cache(str(sample), progress={}, stop_event=stop)
    assert info["stopped"] is True and is_partial_export(data)
    assert not (global_dir / f"{sha}.json").exists()   # partial 绝不发布全局


def test_partial_cache_not_reused_but_visible(tmp_path, sample):
    """partial 缓存工作台可见（read_cached），但 export_to_cache 不复用（重导补全）。"""
    sha = sha256_file(str(sample))
    cache = tmp_path / "cache"
    cache.mkdir()
    partial = json.loads(json.dumps(EXPORT))
    partial["partial"] = True
    partial["meta"]["partial"] = True
    partial["functions"] = partial["functions"][:1]
    (cache / f"{sha}.json").write_text(json.dumps(partial), encoding="utf-8")
    runner, calls = fake_ghidra_runner(tmp_path)
    g = GhidraHeadlessBackend(runner=runner, available=True,
                              tmp_project_dir=tmp_path / "gt")
    svc = DecompilerService(cache_dir=cache, ghidra=g)
    assert svc.read_cached(sha) is not None            # partial 仍可见
    data, _info = svc.export_to_cache(str(sample))
    assert calls["n"] == 1                             # 不复用 partial → 触发重导
    assert len(data["functions"]) == 2                 # 补全为完整导出


# ---------- IDA/Jython 脚本静态护栏（真机才能跑，防 IDA 版本/编码坑回归） ----------

_REPO_ROOT = Path(dc.__file__).resolve().parents[2]


def test_export_scripts_force_utf8_output():
    # 回归：Windows 裸 open 默认 GBK，写回中文命名/注释后重导会产出非 UTF-8 缓存，
    # 后端 read_text(encoding=utf-8) 即 UnicodeDecodeError（v3 缓存统一 UTF-8）。
    ida_src = (_REPO_ROOT / "tools/decompiler/ida/scripts/export_funcs.py").read_text(encoding="utf-8")
    assert 'open(OUT, "w", encoding="utf-8")' in ida_src
    g_src = (_REPO_ROOT / "tools/decompiler/ghidra/scripts/export_funcs.py").read_text(encoding="utf-8")
    assert 'io.open(OUT, "w", encoding="utf-8")' in g_src


def test_ghidra_script_parallel_shard_guards():
    # P2：脚本按 postScript 第二参（worker 数）并行分片；每 worker 独占 DecompInterface；
    # 缺参/非法回退串行；UTF-8 输出不回归（真机才能跑，静态护栏防回归）。
    g_src = (_REPO_ROOT / "tools/decompiler/ghidra/scripts/export_funcs.py").read_text(encoding="utf-8")
    assert "import threading" in g_src
    assert "args[1]" in g_src                      # worker 数取 postScript 第二参
    assert "DecompInterface()" in g_src            # 每 worker 各自实例化解编译器
    assert 'io.open(OUT, "w", encoding="utf-8")' in g_src


def test_ghidra_script_drives_cancellable_analysis():
    """静态护栏（2026-10-01 可中断）：脚本内置自驱分析（AutoAnalysisManager +
    自定义 TaskMonitorAdapter 轮询停止文件）→ 分析阶段可中断 + 报进度；分析阶段
    停止时跳过反编译（仅函数清单 partial）。"""
    g_src = (_REPO_ROOT / "tools/decompiler/ghidra/scripts/export_funcs.py").read_text(
        encoding="utf-8")
    assert "AutoAnalysisManager" in g_src
    assert "TaskMonitorAdapter" in g_src
    assert "args[4]" in g_src                       # 自驱分析开关
    assert "args[5]" in g_src                       # 反汇编内嵌开关（自 args[4] 后移）
    assert "_ANALYSIS_STOPPED" in g_src             # 分析阶段停止 → 跳过反编译
    assert "isCancelled" in g_src and "startAnalysis" in g_src


def test_ghidra_script_control_and_partial_guards():
    # P3：协作式停止（停止标志文件 args[3]）+ 进度文件（args[2]）+ 分片重试 + partial 标记。
    g_src = (_REPO_ROOT / "tools/decompiler/ghidra/scripts/export_funcs.py").read_text(encoding="utf-8")
    assert "args[2]" in g_src and "args[3]" in g_src
    assert "_stop_requested" in g_src and "os.path.exists(STOP)" in g_src
    assert "_write_progress" in g_src
    assert '"partial"' in g_src                      # meta/顶层 partial 标记
    assert "range(2)" in g_src                       # 单函数分片重试一次


def test_ghidra_string_export_supports_listing_iterator():
    g_src = (_REPO_ROOT / "tools/decompiler/ghidra/scripts/export_funcs.py").read_text(
        encoding="utf-8")
    assert "getDefinedData(True)" in g_src
    assert "DefinedDataIterator.definedStrings(prog)" in g_src


def test_apply_script_uses_ida_name_flags_not_idc():
    # IDA 9.3：idc.SN_FORCE 已迁到 ida_name（idc 上取它 AttributeError，idat rc=1 且 stderr 空）
    src = (_REPO_ROOT / "tools/decompiler/ida/scripts/apply_names.py").read_text(encoding="utf-8")
    assert "import ida_name" in src
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    assert "ida_name.SN_FORCE" in code
    assert "idc.SN_FORCE" not in code


# ---------- Agent 工具面：func_kb 机制级查重 ----------

def test_dispatcher_decompile_func_kb_enforcement(tmp_path):
    bb = Blackboard(str(tmp_path / "d.db"))
    project = bb.create_project("dec", "ctf")
    sha = "b" * 64
    sample = tmp_path / "x.elf"
    sample.write_bytes(b"MZ")

    class StubSvc:
        def __init__(self):
            self.calls = 0

        def decompile(self, binary, address=None, name=None):
            self.calls += 1
            return "== f @ 0x1 ==\npseudocode"

    d = ToolDispatcher(bb, gateway=None,
                       project_id=project["id"], session_id="s1", author="s1",
                       decompiler=None)
    # 未装配 → 明确提示，不崩
    assert d.dispatch("decompile", {"binary": str(sample), "address": 1}).startswith("[未装配]")

    stub = StubSvc()
    d2 = ToolDispatcher(bb, gateway=None,
                        project_id=project["id"], session_id="s1", author="s1",
                        decompiler=stub)
    # func_kb 无记录 → 调服务；真文件 sha 写入 func_kb（用真实 hash 保证命中路径一致）
    real_sha = __import__("hashlib").sha256(sample.read_bytes()).hexdigest()
    r1 = d2.dispatch("decompile", {"binary": str(sample), "address": 1})
    assert "pseudocode" in r1 and "bb_upsert_func" in r1 and stub.calls == 1
    # 落库后再次反编译 → 命中缓存，服务不再被调（机制级防重复劳动）
    bb.upsert_func(project["id"], real_sha, 1, "check_flag",
                   analysis="XOR 0x5A 校验", confidence=0.9, analyzed_by="sess-other")
    r2 = d2.dispatch("decompile", {"binary": str(sample), "address": 1})
    assert "func_kb 命中" in r2 and "XOR 0x5A" in r2 and stub.calls == 1
    bb.close()
