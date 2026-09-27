"""反编译组合服务测试（不触网、不依赖真实 Ghidra/IDA）。

覆盖：headless 导出缓存、MCP 在线优先、不可用引导文本、
Agent 工具面的 func_kb 机制级查重。
"""

import json

import pytest

from pathlib import Path

from core.agent.tools import ToolDispatcher
from core.blackboard import Blackboard, TaskQueue
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
                     "func_profile", "entity_query", "server_health", "idb_save"]

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


# ---------- IDA/Jython 脚本静态护栏（真机才能跑，防 IDA 版本/编码坑回归） ----------

_REPO_ROOT = Path(dc.__file__).resolve().parents[2]


def test_export_scripts_force_utf8_output():
    # 回归：Windows 裸 open 默认 GBK，写回中文命名/注释后重导会产出非 UTF-8 缓存，
    # 后端 read_text(encoding=utf-8) 即 UnicodeDecodeError（v3 缓存统一 UTF-8）。
    ida_src = (_REPO_ROOT / "tools/decompiler/ida/scripts/export_funcs.py").read_text(encoding="utf-8")
    assert 'open(OUT, "w", encoding="utf-8")' in ida_src
    g_src = (_REPO_ROOT / "tools/decompiler/ghidra/scripts/export_funcs.py").read_text(encoding="utf-8")
    assert 'io.open(OUT, "w", encoding="utf-8")' in g_src


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

    d = ToolDispatcher(bb, gateway=None, tq=TaskQueue(bb),
                       project_id=project["id"], session_id="s1", author="s1",
                       decompiler=None)
    # 未装配 → 明确提示，不崩
    assert d.dispatch("decompile", {"binary": str(sample), "address": 1}).startswith("[未装配]")

    stub = StubSvc()
    d2 = ToolDispatcher(bb, gateway=None, tq=TaskQueue(bb),
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
