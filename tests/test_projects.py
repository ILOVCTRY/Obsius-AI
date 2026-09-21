"""项目工作区管理测试（DESIGN.md §5.3 定稿布局）。"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import core.projects
from core.blackboard import TaskQueue
from core.projects import ProjectStore, slugify


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(tmp_path / "workspaces")


def test_create_project_layout(store):
    proj = store.create_project("CTF 逆向演练", "ctf", config={"x": 1})
    # 目录布局
    assert (proj.path / "project.json").is_file()
    assert (proj.path / "blackboard.db").is_file()
    for sub in ("samples", "artifacts", "logs"):
        assert (proj.path / sub).is_dir()
    # project.json 元数据（v2：track + capabilities；旧域名 ctf 透明映射）
    meta = json.loads((proj.path / "project.json").read_text(encoding="utf-8"))
    assert meta["name"] == "CTF 逆向演练"      # 中文原名保真
    assert meta["track"] == "ctf"
    assert meta["capabilities"] == ["binary"]  # 旧域名 ctf → ctf 轨 + binary 包
    assert "domain" not in meta                # 写新值，不写旧值
    assert meta["id"].startswith("proj-")
    # 批 2：建项归一化补 autonomy 默认档（ctf=L0），未知键保留
    assert meta["config"]["x"] == 1
    assert meta["config"]["autonomy"]["level"] == "L0"
    assert meta["config"]["autonomy"]["sessions_cap"] == 4
    # 中文目录名 ASCII 化（"CTF 逆向演练" 提取出 ctf）
    assert proj.path.name == meta["slug"]
    assert meta["slug"] == "ctf"
    # 黑板 projects 行同 id（单一真相）
    row = proj.bb.conn.execute("SELECT * FROM projects WHERE id=?", (meta["id"],)).fetchone()
    assert row is not None and row["name"] == "CTF 逆向演练"


def test_relative_store_root_is_resolved(tmp_path, monkeypatch):
    """serve.py 以相对 'workspaces' 启动：proj.path 必须绝对，否则上传端点 relative_to 炸 500。"""
    from core.projects import ProjectStore
    monkeypatch.chdir(tmp_path)
    st = ProjectStore("workspaces")
    p = st.create_project("相对根", "research", ["binary"])
    assert Path(p.path).is_absolute()
    p.close()


def test_slugify_rules():
    assert slugify("Web 渗透 A1", "pentest") == "web-a1"
    assert slugify("  --特殊!@#--", "ctf").startswith("ctf-")  # 无 ASCII → 回退
    s = slugify("x" * 100, "ctf")
    assert len(s) <= 48


def test_slug_collision_suffix(store):
    p1 = store.create_project("demo", "ctf")
    p2 = store.create_project("demo", "ctf")
    assert p1.path != p2.path
    assert p2.path.name.startswith("demo-")
    assert p1.id != p2.id


def test_list_and_open_roundtrip(store):
    p1 = store.create_project("alpha", "ctf")
    p2 = store.create_project("beta-项目", "pentest")  # 旧域名透明映射
    assert p2.track == "pentest" and p2.capabilities == ["web"]
    listed = {m["id"]: m for m in store.list_projects()}
    assert set(listed) == {p1.id, p2.id}
    assert listed[p2.id]["name"] == "beta-项目"
    # 按 slug / id 双口径打开
    assert store.open_project(p1.path.name).id == p1.id
    assert store.open_project(p2.id).name == "beta-项目"
    with pytest.raises(FileNotFoundError):
        store.open_project("nope")
    p1.close()
    p2.close()


def test_legacy_domain_project_json_mapped(store, tmp_path):
    """v1 项目（只有 domain 字段）读取时映射到 track+capabilities（§4.5.5 读兼容）。"""
    from core.projects import Project

    legacy_dir = store.root / "legacy-pentest"
    legacy_dir.mkdir()
    (legacy_dir / "project.json").write_text(json.dumps({
        "id": "proj-legacy0001", "name": "旧渗透项目", "slug": "legacy-pentest",
        "domain": "pentest", "created_at": "2026-01-01T00:00:00+00:00", "config": {},
    }, ensure_ascii=False), encoding="utf-8")
    proj = store.open_project("legacy-pentest")
    assert isinstance(proj, Project)
    assert proj.track == "pentest"
    assert proj.capabilities == ["web"]
    assert proj.domain == "pentest"        # 弃用属性值 = track
    # 旧 ctf domain
    ctf_dir = store.root / "legacy-ctf"
    ctf_dir.mkdir()
    (ctf_dir / "project.json").write_text(json.dumps({
        "id": "proj-legacy0002", "name": "旧 CTF", "slug": "legacy-ctf",
        "domain": "ctf", "config": {},
    }, ensure_ascii=False), encoding="utf-8")
    proj2 = store.open_project("legacy-ctf")
    assert (proj2.track, proj2.capabilities) == ("ctf", ["binary"])
    # 旧 reverse 项目 → research+[binary]（rev-generic 自动归位，不改 project.json）
    rev_dir = store.root / "re1"
    rev_dir.mkdir()
    (rev_dir / "project.json").write_text(json.dumps({
        "id": "proj-legacy0003", "name": "re1", "slug": "re1",
        "domain": "reverse", "config": {},
    }, ensure_ascii=False), encoding="utf-8")
    proj3 = store.open_project("re1")
    assert (proj3.track, proj3.capabilities) == ("research", ["binary"])

    # list_projects / view_meta 的响应也必须补齐（前端 profile 推导依赖）
    listed = {m["id"]: m for m in store.list_projects()}
    assert listed["proj-legacy0001"]["track"] == "pentest"
    assert listed["proj-legacy0001"]["capabilities"] == ["web"]
    assert (listed["proj-legacy0003"]["track"],
            listed["proj-legacy0003"]["capabilities"]) == ("research", ["binary"])
    assert proj.view_meta["track"] == "pentest" and proj.view_meta["capabilities"] == ["web"]
    # 磁盘原样：只读补齐，绝不回写
    assert json.loads((legacy_dir / "project.json").read_text(encoding="utf-8")).get("track") is None


def test_explicit_binding_overrides_legacy(store):
    """新字段齐全时忽略 domain（即便同时残留旧字段）。"""
    p = store.create_project("混搭", "ctf", capabilities=["crypto", "forensics"])
    assert p.track == "ctf" and p.capabilities == ["crypto", "forensics"]
    p.close()


def test_open_survives_reopen_and_data_persists(store):
    p1 = store.create_project("persist", "ctf")
    TaskQueue(p1.bb).publish(p1.id, "队列里的任务", created_by="human")
    p1.close()
    p2 = store.open_project("persist")
    assert len(TaskQueue(p2.bb).list_tasks(p2.id)) == 1  # 数据随项目目录持久化
    p2.close()


def test_corrupt_project_json_skipped(store):
    store.create_project("good", "ctf")
    bad = store.root / "bad"
    bad.mkdir()
    (bad / "project.json").write_text("{broken", encoding="utf-8")
    listed = store.list_projects()
    assert [m["name"] for m in listed] == ["good"]
    with pytest.raises(FileNotFoundError):
        store.open_project("bad")


# ---------- 删除（回收站式，DESIGN.md §5.3） ----------


def test_delete_moves_to_trash(store):
    proj = store.create_project("trash-me", "ctf")
    proj.close()
    trash_path = store.delete_project("trash-me")
    # 原目录没了，移进 .trash/<slug>-<UTC时间戳>/
    assert not proj.path.exists()
    assert trash_path.parent == store.root / ".trash"
    assert trash_path.name.startswith("trash-me-")
    assert trash_path.is_dir()
    # 数据完整保留（不物理删除）
    assert (trash_path / "project.json").is_file()
    assert (trash_path / "blackboard.db").is_file()
    # 列表不再包含
    assert store.list_projects() == []


def test_list_and_open_ignore_trash(store):
    p1 = store.create_project("del-one", "ctf")
    p2 = store.create_project("keep-one", "ctf")
    store.delete_project("del-one", project=p1)
    # 列表只剩存活项目（回收站里的合法 project.json 不得"复活"）
    assert [m["id"] for m in store.list_projects()] == [p2.id]
    # 按已删项目的 slug / id 双口径都打不开
    with pytest.raises(FileNotFoundError):
        store.open_project(p1.path.name)
    with pytest.raises(FileNotFoundError):
        store.open_project(p1.id)
    # 存活项目照常打开
    assert store.open_project("keep-one").id == p2.id


def test_delete_trash_name_conflict(store, monkeypatch):
    proj = store.create_project("dup", "ctf")
    ts = "20260101T000000Z"

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):  # delete_project 的时间戳固定，便于预造同名回收站目录
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(core.projects, "datetime", _Fixed)
    (store.root / ".trash" / f"dup-{ts}").mkdir(parents=True)
    trash_path = store.delete_project("dup", project=proj)
    assert trash_path.name == f"dup-{ts}-2"  # 同名冲突加后缀


def test_list_trashed(store, tmp_path):
    p1 = store.create_project("gone", "ctf")
    p2 = store.create_project("here", "ctf")
    store.delete_project("gone", project=p1)
    trashed = store.list_trashed()
    assert [m["name"] for m in trashed] == ["gone"]
    assert {m["id"] for m in trashed}.isdisjoint({m["id"] for m in store.list_projects()})
    p2.close()
    # 未产生过回收站的 root 不抛错、返回空
    assert ProjectStore(tmp_path / "fresh").list_trashed() == []


def test_delete_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.delete_project("nope")


def test_update_project_track(store):
    """R1 轨退役迁移：update_project_track 改黑板行 track/domain（domain 同步写 track 值）。"""
    proj = store.create_project("旧评估", "assessment", capabilities=["web"])
    pid = proj.id
    # capabilities 省略 → 不动；改 track
    proj.bb.update_project_track(pid, "pentest")
    row = proj.bb.get_project(pid)
    assert row["track"] == "pentest"
    assert row["domain"] == "pentest"  # 旧列同步兜底
    assert row["capabilities"] == ["web"]  # 省略时原样保留
    # 带 capabilities 更新
    proj.bb.update_project_track(pid, "pentest", ["web", "binary"])
    row = proj.bb.get_project(pid)
    assert row["capabilities"] == ["binary", "web"]  # 排序写入
    # 不存在的项目
    with pytest.raises(LookupError):
        proj.bb.update_project_track("proj-0000000000000", "pentest")


def test_normalize_rule_profiles_matrix():
    """F11 rule_profiles 归一化矩阵：合法三态 / 非法 / 未知键剥除 / None→{}。"""
    from core.autonomy import normalize_rule_profiles

    assert normalize_rule_profiles(None) == {}
    assert normalize_rule_profiles({}) == {}
    assert normalize_rule_profiles({"owners": "*"}) == {"owners": "*"}
    assert normalize_rule_profiles({"rating": []}) == {"rating": []}
    assert normalize_rule_profiles({"owners": ["b", "a", "b", " "]}) == {"owners": ["b", "a"]}
    assert normalize_rule_profiles({"owners": "*", "rating": ["x"]}) == {
        "owners": "*", "rating": ["x"]}
    # 未知键剥除
    assert normalize_rule_profiles({"owners": "*", "junk": 1}) == {"owners": "*"}
    # 非法形态
    with pytest.raises(ValueError):
        normalize_rule_profiles({"owners": "all"})
    with pytest.raises(ValueError):
        normalize_rule_profiles({"owners": [1, 2]})
    with pytest.raises(ValueError):
        normalize_rule_profiles({"rating": "*"})
    with pytest.raises(ValueError):
        normalize_rule_profiles("bad")
    # 全空白 tag strip 后被剔除 → 合法空清单（非异常）
    assert normalize_rule_profiles({"rating": ["  "]}) == {"rating": []}


def test_update_config_rule_profiles(store):
    """rule_profiles PATCH：非法 422（ValueError）；合法双写一致；null 清键恢复缺省态。"""
    proj = store.create_project("rp", "pentest", capabilities=["web"])
    pid = proj.id
    # 合法：写入并双写
    store.update_config(pid, {"rule_profiles": {"owners": "*", "rating": ["edu-rating"]}})
    meta = json.loads((proj.path / "project.json").read_text(encoding="utf-8"))
    assert meta["config"]["rule_profiles"] == {"owners": "*", "rating": ["edu-rating"]}
    assert proj.bb.get_project(pid)["config"]["rule_profiles"] == {
        "owners": "*", "rating": ["edu-rating"]}
    # 归一化写入（去重保序）
    store.update_config(pid, {"rule_profiles": {"rating": ["b", "a", "b"]}})
    assert proj.bb.get_project(pid)["config"]["rule_profiles"] == {"rating": ["b", "a"]}
    # 非法 → ValueError（API 层转 422）
    with pytest.raises(ValueError):
        store.update_config(pid, {"rule_profiles": {"owners": "all"}})
    # 空对象 / null → 剥键恢复缺省态
    store.update_config(pid, {"rule_profiles": {}})
    meta = json.loads((proj.path / "project.json").read_text(encoding="utf-8"))
    assert "rule_profiles" not in meta["config"]
    store.update_config(pid, {"rule_profiles": {"rating": ["x"]}})
    assert "rule_profiles" in proj.bb.get_project(pid)["config"]
    store.update_config(pid, {"rule_profiles": None})
    assert "rule_profiles" not in proj.bb.get_project(pid)["config"]
    assert "rule_profiles" not in json.loads(
        (proj.path / "project.json").read_text(encoding="utf-8"))["config"]
    proj.close()


def test_close_blocks_bb_reinstantiation(store):
    """close() 后 proj.bb 必须抛 BlackboardClosedError，不得惰性重建黑板。

    删除中 WS tick 仍持有旧 Project 引用：旧实现里 proj.close() 把 _bb 置 None，
    下次 proj.bb 访问会新建 Blackboard（目录移走后 OperationalError，移走前重新锁库）。"""
    from core.blackboard.store import BlackboardClosedError
    proj = store.create_project("seal", "ctf")
    assert proj.bb is proj.bb  # 惰性建过一次
    proj.close()
    with pytest.raises(BlackboardClosedError):
        _ = proj.bb
    # 新实例不受影响（重开项目走 open_project 新对象）
    assert store.open_project("seal").name == "seal"
