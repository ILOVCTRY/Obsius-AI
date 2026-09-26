"""检索黄金集回归（retrieval-upgrade M3，2026-09-23）。

三层匹配链（route_index 静态导航 → kbindex 2-gram+同义词扩展 → 提示行）以
recall@4 阈值回归：匹配链任何改动（词表/索引/停用词/路由）必须跑本测试。
对真实 packs/kb 语料标注（tests/fixtures/retrieval-golden.yaml）——语料即被测物。
"""

from pathlib import Path

import yaml

from core.skills.kbindex import kb_module_hints

PACKS_ROOT = Path(__file__).resolve().parent.parent / "packs"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "retrieval-golden.yaml"
RECALL_AT_4_THRESHOLD = 0.8


def _load() -> list[dict]:
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["entries"]


def test_retrieval_golden_recall_at_4():
    """黄金集 recall@4 ≥ 阈值；失败时打印 miss 明细供词表/标注审阅。"""
    entries = _load()
    assert len(entries) >= 20, "黄金集不足 20 条——请补充标注"
    miss: list[str] = []
    for e in entries:
        hints = kb_module_hints(PACKS_ROOT, [e["cap"]], e["query"], cap=4)
        modules = [h.module for h, _sec in hints]
        if not any(m in e["expect"] for m in modules):
            miss.append(f"[{e['cap']}] {e['query']!r} → {modules}\n"
                        f"    期望命中: {e['expect']}")
    recall = 1 - len(miss) / len(entries)
    assert recall >= RECALL_AT_4_THRESHOLD, (
        f"黄金集 recall@4={recall:.2f} < {RECALL_AT_4_THRESHOLD}\n"
        + "\n".join(miss))


def test_retrieval_golden_expect_paths_exist():
    """黄金集标注自检：expect 路径必须真实存在于 packs/kb——防语料漂移后
    黄金集悄悄失效。"""
    for e in _load():
        for m in e["expect"]:
            assert (PACKS_ROOT / "kb" / m).is_file(), \
                f"黄金集 expect 路径不存在: {m}（语料漂移，请更新标注）"
