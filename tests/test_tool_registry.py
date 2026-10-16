"""工具注册表测试（tool-registry，2026-10-03）。

钉死三件事：
1. 注册表内部自洽（group 合法、handler 存在、name 无重复）；
2. **派生常量与迁移前快照逐集合相等**——flags 标注错一个就改变闸门放行面，
   这是本次重构最大的回归面，故用迁移前 tools.py 的原始集合硬编码对照；
3. 装饰器/查表的失败语义（重名、非法 group、未知工具回落「其他」）。
"""

import pytest

from core.agent import tool_registry as R
from core.agent.tools import ToolDispatcher

# ---- 基线快照（2026-10-06 任务机制退役后的工具面；六个任务工具已删） ----
_SNAPSHOT = {
    "_CONTROL_TOOLS": {
        "finish", "request_steps", "close_intent", "reopen_intent",
    },
    "_PLAN_TOOLS": {"declare_intent"},
    "_PLAN_PRE_ALLOWED": {
        "declare_intent",
        "bb_query", "kb_open", "kb_search", "route_lookup", "list_symbols",
        "decompile", "strings_search", "func_xrefs", "disasm", "read_file",
        "search_files", "skill_open", "browser_navigate", "browser_screenshot",
        "browser_content",
    },
    "_INTENT_FLOW_TOOLS": {
        "declare_intent", "close_intent", "reopen_intent", "bb_delete_intent",
        "bb_add_finding", "bb_update_finding", "bb_delete_finding",
        "bb_delete_asset", "bb_delete_assets", "bb_merge_assets",
        "bb_declare_challenge",
        "finish", "request_steps",
    },
    "_INTENT_PRE_ALLOWED": {
        "declare_intent",
        "bb_query", "kb_open", "kb_search", "route_lookup", "list_symbols",
        "decompile", "strings_search", "func_xrefs", "disasm", "read_file",
        "search_files", "skill_open", "browser_navigate", "browser_screenshot",
        "browser_content",
        "finish", "request_steps",
        "close_intent", "reopen_intent",
        "bb_delete_intent", "bb_add_finding", "bb_update_finding",
        "bb_delete_finding", "bb_delete_asset", "bb_delete_assets",
        "bb_merge_assets", "bb_declare_challenge",
        "bb_notify",
        "request_authorization", "propose_pack_edit", "bb_add_asset",
    },
    "_SPILL_SKIP": {"kb_open", "skill_open", "route_lookup"},
    "_COLLAB_TOOLS": {"request_authorization"},
    "_KNOWLEDGE_EXTRA": {
        "kb_open", "kb_search", "skill_open", "route_lookup",
        "propose_pack_edit", "list_symbols", "decompile",
        "strings_search", "func_xrefs", "disasm",
    },
    "_FILE_TOOLS": {"read_file", "search_files"},
}

# 退役后 AGENT_TOOLS 的工具名（顺序即文件顺序，Agent schema 顺序对外可见）
# 2026-10-14：+bb_declare_challenge（challenge-asset v38，CTF 题目登记，仅主控）
_SNAPSHOT_ORDER = [
    "run_cmd", "read_file", "search_files",
    "bb_add_asset", "bb_declare_challenge", "bb_delete_asset", "bb_delete_assets",
    "bb_merge_assets", "bb_asset_status",
    "bb_add_finding", "bb_update_finding", "bb_delete_finding", "bb_add_artifact",
    "bb_query", "kb_open", "kb_search", "propose_pack_edit",
    "bb_upsert_func", "bb_blueprint_create", "bb_blueprint_update",
    "bb_feature_tree_create", "bb_feature_node",
    "bb_logic_block_create", "bb_logic_block_update",
    "decompile", "list_symbols", "strings_search", "func_xrefs", "disasm",
    "bb_notify",
    "finish", "request_steps",
    "browser_replay", "browser_intruder",
    "browser_navigate", "browser_click", "browser_type", "browser_screenshot",
    "browser_content", "browser_back",
    "route_lookup", "skill_open",
    "request_authorization",
    "declare_intent", "close_intent", "reopen_intent", "bb_delete_intent",
]


# ---------- 注册表内部自洽 ----------

def test_registry_matches_pre_migration_tool_order():
    assert [t["name"] for t in R.AGENT_TOOLS] == _SNAPSHOT_ORDER
    assert list(R.REGISTRY) == _SNAPSHOT_ORDER


def test_every_tool_has_legal_group():
    """新增工具必须显式落组——「其他」是漏配信号，API 目录断言也拦它。"""
    for spec in R.REGISTRY.values():
        assert spec.group in R.TOOL_GROUPS, f"{spec.name} 落组非法: {spec.group!r}"
        assert spec.group != "其他", f"{spec.name} 未落具体组"


def test_every_handler_exists_on_dispatcher():
    """注册表与实现不得漂移（tools.py 导入期自检的同口径断言，测试侧再钉一遍）。"""
    missing = [s.handler for s in R.REGISTRY.values()
               if not callable(getattr(ToolDispatcher, s.handler, None))]
    assert missing == []


def test_default_handler_follows_naming_convention():
    for spec in R.REGISTRY.values():
        assert spec.handler == f"_tool_{spec.name}"


def test_schema_shape():
    for spec in R.REGISTRY.values():
        schema = spec.as_schema()
        assert set(schema) == {"name", "description", "input_schema"}
        assert isinstance(schema["description"], str) and schema["description"]
        assert schema["input_schema"].get("type") == "object"


# ---------- 派生常量 ≡ 迁移前快照 ----------

@pytest.mark.parametrize("const", sorted(_SNAPSHOT))
def test_derived_constant_equals_pre_migration_snapshot(const):
    """flags 标注错一个就静默改变闸门放行面——逐集合对照迁移前快照。"""
    assert set(getattr(R, const)) == _SNAPSHOT[const]


def test_tool_groups_unchanged():
    assert R.TOOL_GROUPS == ["执行", "文件", "黑板", "知识", "浏览器", "协作",
                             "计划", "控制"]


def test_plan_gate_is_derived_not_hardcoded():
    """_PLAN_PRE_ALLOWED 必须是「计划原语 ∪ plan_pre 标记」的推导，不是硬编码并集。"""
    assert R._PLAN_TOOLS <= R._PLAN_PRE_ALLOWED
    assert R._PLAN_PRE_ALLOWED == R._PLAN_TOOLS | {
        s.name for s in R.REGISTRY.values() if "plan_pre" in s.flags}


def test_intent_gate_is_derived_not_hardcoded():
    assert R._INTENT_PRE_ALLOWED == (
        R._PLAN_PRE_ALLOWED | R._CONTROL_TOOLS | R._INTENT_FLOW_TOOLS
        | {s.name for s in R.REGISTRY.values() if "intent_pre" in s.flags})


def test_group_mapping_matches_legacy_prefix_rules():
    """迁移前 agent_tool_group 是前缀+集合规则；迁移后落组结果须逐条一致。"""
    expected = {
        "request_steps": "控制",
        "declare_intent": "计划",
        "request_authorization": "协作",
        "run_cmd": "执行",
        "read_file": "文件", "search_files": "文件",
        "bb_query": "黑板", "bb_delete_intent": "黑板", "bb_notify": "黑板",
        "browser_navigate": "浏览器", "browser_back": "浏览器",
        "kb_open": "知识", "decompile": "知识", "disasm": "知识",
    }
    for name, group in expected.items():
        assert R.agent_tool_group(name) == group, name


def test_agent_tool_group_unknown_falls_back_to_other():
    assert R.agent_tool_group("no_such_tool") == "其他"


# ---------- 装饰器失败语义 ----------

def test_duplicate_registration_rejected():
    with pytest.raises(ValueError, match="重复注册"):
        R.tool("run_cmd", description="x", input_schema={"type": "object"},
               group="执行")


def test_illegal_group_rejected():
    with pytest.raises(ValueError, match="group 非法"):
        R.tool("__tmp_tool__", description="x", input_schema={"type": "object"},
               group="其他")
    assert "__tmp_tool__" not in R.REGISTRY


# ---------- bb_query what 面按轨裁剪（track-query-split，2026-10-13） ----------

def test_bb_query_what_families_partition_full_no_crossover():
    """两家族并集=全量九面；交集只剩三公共面（findings/assets/events）——
    渗透与逆向互用不到的面不允许同时出现在两个家族里。"""
    rev = set(R._BB_QUERY_WHAT_REVERSE)
    pen = set(R._BB_QUERY_WHAT_PENTEST)
    assert rev | pen == set(R._BB_QUERY_WHAT_FULL)
    assert rev & pen == {"findings", "assets", "events"}
    # 逆向家族持有功能树/业务块/site 独占与删除的边界
    assert "site" not in rev and "feature_tree" in rev and "logic_block" in rev
    assert "site" in pen and "func" not in pen


def test_bb_query_what_for_track_mapping():
    assert R.bb_query_what_for("research") == list(R._BB_QUERY_WHAT_REVERSE)
    assert R.bb_query_what_for("ctf") == list(R._BB_QUERY_WHAT_REVERSE)
    assert R.bb_query_what_for("pentest") == list(R._BB_QUERY_WHAT_PENTEST)
    assert R.bb_query_what_for("redteam") == list(R._BB_QUERY_WHAT_PENTEST)
    # 未知/空/None 轨=全量回退（行为与裁剪前一致）
    for track in ("", None, "mystery", "  research  "[:0] + "assessment"):
        assert R.bb_query_what_for(track) == list(R._BB_QUERY_WHAT_FULL)


def test_bb_query_schema_for_reverse_keeps_reverse_faces_only():
    for track in ("research", "ctf"):
        s = R.bb_query_schema_for(track)
        enum = s["input_schema"]["properties"]["what"]["enum"]
        assert "site" not in enum
        assert {"func", "blueprint", "package", "feature_tree",
                "logic_block"} <= set(enum)
        # 逆向专属参数保留（含 v36 新增 tree_id/block_id）
        assert {"binary_sha256", "tree_id", "block_id", "package_id",
                "blueprint_id"} <= set(s["input_schema"]["properties"])
        assert "逆向 family" in s["description"]


def test_bb_query_schema_for_pentest_drops_reverse_only_faces():
    for track in ("pentest", "redteam"):
        s = R.bb_query_schema_for(track)
        enum = s["input_schema"]["properties"]["what"]["enum"]
        assert "site" in enum
        for face in ("func", "blueprint", "package", "feature_tree", "logic_block"):
            assert face not in enum
        # 逆向专属参数整组删掉——模型看不见就不会传
        for k in R._BB_QUERY_REVERSE_ONLY_PROPS:
            assert k not in s["input_schema"]["properties"]
        assert "渗透 family" in s["description"]


def test_bb_query_schema_for_unknown_track_equals_canonical():
    full = {t["name"]: t for t in R.AGENT_TOOLS}["bb_query"]
    assert R.bb_query_schema_for("mystery") == full
    assert R.bb_query_schema_for(None) == full


def test_bb_query_schema_for_returns_fresh_dict_no_registry_mutation():
    """变体 schema 是新 dict——裁剪消费方（AgentSession/chat runtime）随便改
    不污染注册表；registry 的 canonical 项恒为全量。"""
    before = {t["name"]: t for t in R.AGENT_TOOLS}["bb_query"]
    s = R.bb_query_schema_for("research")
    s["input_schema"]["properties"]["what"]["enum"].append("__x__")
    s["input_schema"]["properties"].pop("tree_id", None)
    assert {t["name"]: t for t in R.AGENT_TOOLS}["bb_query"] == before
    assert R.bb_query_schema_for("research")["input_schema"]["properties"] \
        ["what"]["enum"] == list(R._BB_QUERY_WHAT_REVERSE)
