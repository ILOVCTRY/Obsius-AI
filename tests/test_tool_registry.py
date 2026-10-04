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

# ---- 迁移前快照（逐字抄自 2026-10-03 重构前的 core/agent/tools.py） ----
_SNAPSHOT = {
    "_CONTROL_TOOLS": {
        "complete_task", "fail_task", "finish", "request_steps",
        "close_intent", "reopen_intent",
    },
    "_PLAN_TOOLS": {"task_plan", "task_step", "task_reconcile", "declare_intent"},
    "_PLAN_PRE_ALLOWED": {
        "task_plan", "task_step", "task_reconcile", "declare_intent",
        "bb_query", "kb_open", "kb_search", "route_lookup", "list_symbols",
        "decompile", "strings_search", "func_xrefs", "disasm", "read_file",
        "search_files", "skill_open", "browser_navigate", "browser_screenshot",
        "browser_content",
    },
    "_INTENT_FLOW_TOOLS": {
        "declare_intent", "close_intent", "reopen_intent", "bb_delete_intent",
        "bb_add_finding", "bb_update_finding", "bb_delete_finding",
        "bb_delete_asset", "bb_merge_assets",
        "task_plan", "task_step", "task_reconcile",
        "complete_task", "fail_task", "finish", "request_steps",
    },
    "_INTENT_PRE_ALLOWED": {
        "task_plan", "task_step", "task_reconcile", "declare_intent",
        "bb_query", "kb_open", "kb_search", "route_lookup", "list_symbols",
        "decompile", "strings_search", "func_xrefs", "disasm", "read_file",
        "search_files", "skill_open", "browser_navigate", "browser_screenshot",
        "browser_content",
        "complete_task", "fail_task", "finish", "request_steps",
        "close_intent", "reopen_intent",
        "bb_delete_intent", "bb_add_finding", "bb_update_finding",
        "bb_delete_finding", "bb_delete_asset", "bb_merge_assets",
        "publish_task", "bb_notify",
        "request_authorization", "propose_pack_edit", "bb_add_asset",
    },
    "_SPILL_SKIP": {"kb_open", "skill_open", "route_lookup"},
    "_COLLAB_TOOLS": {"publish_task", "request_authorization"},
    "_KNOWLEDGE_EXTRA": {
        "kb_open", "kb_search", "skill_open", "route_lookup",
        "propose_pack_edit", "list_symbols", "decompile",
        "strings_search", "func_xrefs", "disasm",
    },
    "_FILE_TOOLS": {"read_file", "search_files"},
}

# 迁移前 AGENT_TOOLS 的 48 个工具名（顺序即文件顺序，Agent schema 顺序对外可见）
_SNAPSHOT_ORDER = [
    "run_cmd", "read_file", "search_files",
    "bb_add_asset", "bb_delete_asset", "bb_merge_assets", "bb_asset_status",
    "bb_add_finding", "bb_update_finding", "bb_delete_finding", "bb_add_artifact",
    "bb_query", "kb_open", "kb_search", "propose_pack_edit",
    "bb_upsert_func", "bb_blueprint_create", "bb_blueprint_update",
    "bb_logic_block_create", "bb_logic_block_update",
    "decompile", "list_symbols", "strings_search", "func_xrefs", "disasm",
    "task_plan", "task_step", "task_reconcile",
    "publish_task", "bb_notify",
    "complete_task", "fail_task", "finish", "request_steps",
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
        "complete_task": "控制", "request_steps": "控制",
        "task_plan": "计划", "task_reconcile": "计划",
        "publish_task": "协作",
        "run_cmd": "执行",
        "read_file": "文件", "search_files": "文件",
        "bb_query": "黑板", "bb_delete_intent": "黑板",
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
