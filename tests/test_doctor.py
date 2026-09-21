"""pack doctor 测试：tmp packs 造结构，精确断言 error/warning/info 分级。"""

from core.skills.doctor import diagnose

_SKILL = """---
name: {name}
description: {desc}
---
# {name}
{body}
"""


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_packs(root) -> None:
    # web 包：齐全技能/红线/快照；一个技能 name 不一致（error）
    _write(root / "capabilities/web/pack.yaml", "kind: capability\nname: web\n")
    _write(root / "capabilities/web/rules/redlines.md", "# web 红线\n")
    _write(root / "kb/web/ctf-web/auth-jwt.md", "# jwt\n")  # M0 kb 全局单根
    _write(root / "capabilities/web/skills/good-skill/SKILL.md",
           _SKILL.format(name="good-skill", desc="好技能", body="正文"))
    _write(root / "capabilities/web/skills/wrong-dir/SKILL.md",
           _SKILL.format(name="actually-other-name", desc="名实不符", body="正文"))
    _write(root / "capabilities/web/skills/broken-refs/SKILL.md",
           _SKILL.format(name="broken-refs", desc="引用失效", body=(
               "见 `kb_open(module=\"ctf-web/missing.md\")`。")))
    _write(root / "capabilities/web/skills/disabled-skill/SKILL.md",
           "---\nname: disabled-skill\ndescription: 停用\nenabled: false\n---\n# x\n")

    # crypto 包：缺 redlines（warning）+ 无 kb 域目录（M0 info 属预期）；
    # 其技能引用未知快照 → kb-snapshot-unknown（引用方域无 kb，无法归属）
    _write(root / "capabilities/crypto/pack.yaml", "kind: capability\nname: crypto\n")
    _write(root / "capabilities/crypto/skills/crypto-broken/SKILL.md",
           _SKILL.format(name="crypto-broken", desc="未知快照引用",
                         body="另引 `kb_open(module=\"nope-snap/x.md\")`。"))

    # ctf 轨：角色引用悬空技能/禁用技能/未注册 task_type
    _write(root / "tracks/ctf/track.yaml", "name: ctf\n")
    _write(root / "tracks/ctf/rules/redlines.md", "# ctf 红线\n")
    _write(root / "tracks/ctf/task_types.yaml", "generic: passive\nsolve: passive\n")
    _write(root / "tracks/ctf/roles/_generalist.yaml", 'name: _generalist\nskills: null\n')
    _write(root / "tracks/ctf/roles/recon.yaml",
           'name: recon\n'
           'skills: [good-skill, ghost-skill, disabled-skill]\n'
           'task_types: [solve, bogus-type]\n')
    # 回收站有一项（info）
    _write(root / "tracks/ctf/roles/.history/trash/old-role.20260101T000000Z.bak",
           "name: old-role\n")

    # research 空轨：缺 task_types.yaml 与 redlines（warning），不应炸
    _write(root / "tracks/research/track.yaml", "name: research\n")


def test_doctor_levels_and_codes(tmp_path):
    root = tmp_path / "packs"
    _build_packs(root)
    rep = diagnose(root)
    issues = rep.issues
    by_code: dict[str, list] = {}
    for i in issues:
        by_code.setdefault(i.code, []).append(i)

    errors = {i.code for i in issues if i.level == "error"}
    warnings = {i.code for i in issues if i.level == "warning"}
    infos = {i.code for i in issues if i.level == "info"}

    # error 三项绑定断裂（M0：kb_sources 退役，kb-source-root-missing 检查随之移除）
    assert "role-skill-missing" in errors
    assert "role-tasktype-unregistered" in errors
    assert "skill-name-mismatch" in errors
    assert any("ghost-skill" in i.message for i in by_code["role-skill-missing"])
    assert any("bogus-type" in i.message for i in by_code["role-tasktype-unregistered"])

    # warning：禁用引用 / 缺红线 / kb 引用失效（含未知快照）/ 空轨缺注册表
    assert "role-skill-disabled" in warnings
    assert "missing-redlines" in warnings
    assert "kb-module-broken" in warnings
    assert "kb-snapshot-unknown" in warnings
    assert "missing-task-types" in warnings
    assert any("ctf-web/missing.md" in i.message for i in by_code["kb-module-broken"])
    assert any("nope-snap/x.md" in i.message for i in by_code["kb-snapshot-unknown"])
    # crypto 与 research 各缺一条红线
    redline_targets = {i.target for i in by_code["missing-redlines"]}
    assert "capabilities/crypto/rules/redlines.md" in redline_targets
    assert "tracks/research/rules/redlines.md" in redline_targets
    assert "capabilities/web/rules/redlines.md" not in redline_targets

    # info：孤儿技能（被引用的 good-skill 不报；禁用技能不报）+ 回收站
    orphan = {i.message for i in by_code.get("orphan-skill", [])}
    assert all("good-skill" not in m for m in orphan)
    assert all("disabled-skill" not in m for m in orphan)
    assert "trash-present" in infos

    # counts 与序列化
    counts = rep.counts
    assert counts["error"] >= 3 and counts["warning"] >= 6 and counts["info"] >= 2
    payload = rep.to_dict()
    assert set(payload["counts"]) == {"error", "warning", "info"}
    assert payload["issues"] == sorted(
        payload["issues"],
        key=lambda x: ({"error": 0, "warning": 1, "info": 2}[x["level"]],
                       x["code"], x["target"]))


def test_doctor_clean_pack_has_only_soft_notes(tmp_path):
    """健康结构：零 error；kb 引用全部有效；引用得到的技能不报孤儿。"""
    root = tmp_path / "packs"
    _write(root / "capabilities/web/pack.yaml", "kind: capability\nname: web\n")
    _write(root / "capabilities/web/rules/redlines.md", "# r\n")
    _write(root / "kb/web/ctf-web/auth-jwt.md", "# jwt\n")
    _write(root / "capabilities/web/skills/s1/SKILL.md",
           '---\nname: s1\ndescription: d\n---\n# s1\n`kb_open(module="ctf-web/auth-jwt.md")`\n')
    _write(root / "tracks/ctf/track.yaml", "name: ctf\n")
    _write(root / "tracks/ctf/rules/redlines.md", "# r\n")
    _write(root / "tracks/ctf/task_types.yaml", "generic: passive\nsolve: passive\n")
    _write(root / "tracks/ctf/roles/recon.yaml",
           "name: recon\nskills: [s1]\ntask_types: [solve]\n")
    rep = diagnose(root)
    assert rep.counts["error"] == 0
    codes = {i.code for i in rep.issues}
    assert not {"kb-module-broken", "kb-snapshot-unknown", "orphan-skill"} & codes


def test_doctor_role_display_name_decoupled_from_stem(tmp_path):
    """yaml name 行=中文显示名（可 ≠ 文件 stem）：不报 mismatch；缺 name 行才 warning。"""
    root = tmp_path / "packs"
    _write(root / "capabilities/web/pack.yaml", "kind: capability\nname: web\n")
    _write(root / "capabilities/web/rules/redlines.md", "# r\n")
    _write(root / "tracks/ctf/track.yaml", "name: ctf\n")
    _write(root / "tracks/ctf/rules/redlines.md", "# r\n")
    _write(root / "tracks/ctf/task_types.yaml", "generic: passive\n")
    _write(root / "tracks/ctf/roles/_generalist.yaml", 'name: 通用\nskills: null\n')
    _write(root / "tracks/ctf/roles/noname.yaml", "skills: null\n")  # 缺 name 行
    rep = diagnose(root)
    codes = {i.code for i in rep.issues}
    assert "role-name-mismatch" not in codes          # 旧检查已移除（name≠stem 合法）
    missing = [i for i in rep.issues if i.code == "role-name-missing"]
    assert len(missing) == 1 and "noname" in missing[0].target


def test_doctor_rating_without_owner(tmp_path):
    """F11：rating/<tag>.md 存在但 owners/<tag>.md 缺失 → warning；同存 → 不告警。"""
    root = tmp_path / "packs"
    _write(root / "tracks/pentest/track.yaml", "name: pentest\n")
    _write(root / "tracks/pentest/rules/rating/osrc.md", "# osrc 评级与价值口径\n")
    rep = diagnose(root)
    hits = [i for i in rep.issues if i.code == "rating-without-owner"]
    assert len(hits) == 1 and hits[0].level == "warning"
    assert "osrc" in hits[0].message and hits[0].target.endswith("rating/osrc.md")
    # 补齐同名 owners → 告警消失
    _write(root / "tracks/pentest/rules/owners/osrc.md", "# osrc 授权与红线\n")
    rep2 = diagnose(root)
    assert not [i for i in rep2.issues if i.code == "rating-without-owner"]


def test_doctor_missing_root(tmp_path):
    rep = diagnose(tmp_path / "nope")
    assert rep.counts["error"] == 1
    assert rep.issues[0].code == "packs-root-missing"
