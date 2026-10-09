"""项目工作区管理（DESIGN.md §5.3 定稿布局）。

项目 = workspaces/<slug>/ 一个文件夹：
  project.json（元数据）+ blackboard.db（本项目全部黑板数据）
  + samples/（人放的 untrusted 输入）+ artifacts/（Agent 产物）+ logs/（可选）

原则：
- 每项目一个 SQLite 库；归档/备份 = 操作文件夹。
- 删除 = 回收站式（DESIGN.md §5.3）：整目录 rename 进 workspaces/.trash/，
  不物理删除；恢复 = 手动移回；所有扫描忽略 .trash。
- 不建全局注册表：项目列表 = 扫描 workspaces/*/project.json（单一真相源）。
- id 由本模块统一生成，blackboard projects 行同 id。
"""

import fnmatch
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.autonomy import normalize_autonomy
from core.blackboard.store import Blackboard, BlackboardClosedError
from core.skills.taxonomy import LEGACY_DOMAIN_MAP, project_binding

PROJECT_FILE = "project.json"
DB_FILE = "blackboard.db"
SUBDIRS = ("samples", "artifacts", "logs")
TRASH_DIR = ".trash"
WORKSPACE_TREE_MAX_DEPTH = 4
WORKSPACE_TREE_MAX_NODES = 1000
WORKSPACE_TREE_MAX_CHILDREN = 200
_WORKSPACE_HIDDEN_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules",
                          "__pycache__", ".history", ".trash", ".tmp"}
_WORKSPACE_HIDDEN_NAMES = {"blackboard.db", "blackboard.db-wal", "blackboard.db-shm",
                           "project.json"}
_WORKSPACE_HIDDEN_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
                              "id_rsa*", "credentials*", "secrets*")


def _workspace_hidden(name: str, is_dir: bool) -> bool:
    if is_dir and name in _WORKSPACE_HIDDEN_DIRS:
        return True
    if name in _WORKSPACE_HIDDEN_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in _WORKSPACE_HIDDEN_PATTERNS)


def list_workspace_tree(root: str | Path, *, max_depth: int = WORKSPACE_TREE_MAX_DEPTH,
                        max_nodes: int = WORKSPACE_TREE_MAX_NODES,
                        max_children: int = WORKSPACE_TREE_MAX_CHILDREN) -> dict:
    """返回受限项目工作区文件树；只返回相对路径和元数据，不跟随符号链接。"""
    base = Path(root).resolve()
    nodes_seen = 0
    truncated = False

    def walk(directory: Path, rel_prefix: str, depth: int) -> list[dict]:
        nonlocal nodes_seen, truncated
        if depth > max_depth or nodes_seen >= max_nodes:
            truncated = True
            return []
        try:
            entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return []
        out: list[dict] = []
        for entry in entries:
            if len(out) >= max_children or nodes_seen >= max_nodes:
                truncated = True
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if _workspace_hidden(entry.name, is_dir) or entry.is_symlink():
                    continue
                rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
                nodes_seen += 1
                node: dict = {"name": entry.name, "path": rel, "kind": "dir" if is_dir else "file"}
                if is_dir:
                    if depth < max_depth:
                        node["children"] = walk(Path(entry.path), rel, depth + 1)
                    else:
                        truncated = True
                else:
                    try:
                        node["size"] = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        node["size"] = 0
                out.append(node)
            except OSError:
                continue
        return out

    return {"root": "workspace", "nodes": walk(base, "", 0), "truncated": truncated,
            "limits": {"max_depth": max_depth, "max_nodes": max_nodes,
                       "max_children": max_children}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def view_meta(meta: dict) -> dict:
    """API 读视图：旧 domain-only 项目补 track/capabilities（只补响应，不改磁盘；§4.5.5）。"""
    if meta.get("track"):
        return meta
    track, caps = project_binding(meta)
    out = dict(meta)
    out["track"] = track
    out["capabilities"] = list(meta.get("capabilities") or caps)
    return out


def slugify(name: str, track: str) -> str:
    """目录 slug：ASCII 化（容器挂载友好）；中文等无 ASCII 字符时回退 track-日期-随机。"""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        s = f"{track}-{date}-{uuid.uuid4().hex[:4]}"
    return s[:48]


class Project:
    """已打开的项目句柄：meta + 黑板连接 + 各目录路径。"""

    def __init__(self, root: Path):
        self.path = root
        self.meta: dict = json.loads((root / PROJECT_FILE).read_text(encoding="utf-8"))
        self._bb: Blackboard | None = None
        # close()/close_all() 后置位：bb property 拒绝重新实例化。
        # 否则删除中仍持 Project 引用的线程（WS tick）访问 proj.bb 会新建
        # Blackboard（目录移走后 OperationalError，移走前重新锁死 db）。
        self._closed = False

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def track(self) -> str:
        """场景轨（单选）。旧 project.json 只有 domain 时经映射表读兼容。"""
        return project_binding(self.meta)[0]

    @property
    def capabilities(self) -> list[str]:
        """启用能力包（多选）。专家绑定项目的运行时可见范围用 caps_effective 推导
        （app 层 core.skills.experts.caps_effective），本属性恒返回盘上原始绑定。"""
        return project_binding(self.meta)[1]

    @property
    def experts(self) -> list[str]:
        """绑定专家 id 清单（expert-pool M2，§4.4）。无绑定（存量项目）= 空表。"""
        return [e for e in (self.meta.get("experts") or []) if str(e).strip()]

    @property
    def domain(self) -> str:
        """已弃用语义，保留属性兼容旧调用方；值 = track。"""
        return self.track

    @property
    def view_meta(self) -> dict:
        """供 API 序列化：旧 domain 项目在响应里补齐 track/capabilities（不落盘）。"""
        return view_meta(self.meta)

    @property
    def bb(self) -> Blackboard:
        if self._closed:
            raise BlackboardClosedError(
                f"项目句柄已关闭（删除中），拒绝重开黑板: {self.path}")
        if self._bb is None:
            self._bb = Blackboard(str(self.path / DB_FILE))
        return self._bb

    @property
    def samples_dir(self) -> Path:
        return self.path / "samples"

    @property
    def artifacts_dir(self) -> Path:
        return self.path / "artifacts"

    def close(self) -> None:
        if self._bb is not None:
            self._bb.close()
            self._bb = None
        self._closed = True


class ProjectStore:
    """workspaces 根目录的项目管理：创建 / 列出 / 打开。"""

    def __init__(self, root: str | Path = "workspaces"):
        # 始终绝对化：serve.py 以 "workspaces" 相对根启动时，_inside() 的 resolve 路径
        # 才能与 proj.path 做 relative_to（否则样本/日志上传 500）。
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 创建 ----------

    def create_project(
        self,
        name: str,
        track: str = "ctf",
        capabilities: list[str] | None = None,
        config: dict | None = None,
        experts: list[str] | None = None,
    ) -> Project:
        """创建项目目录与黑板库。id 全局唯一（project.json 与 projects 行同 id）。

        v2 绑定：track（场景轨单选）+ capabilities（能力包多选）。
        旧域名（pentest/ctf）作 track 传入时透明映射（读兼容、写新值，§4.5.5）。
        专家绑定（expert-pool M2，§4.4）：experts 非空才写 meta["experts"]
        （缺省不写键 = 存量直通语义，caps_effective 走 meta.capabilities）；
        专家存在性/轨校验在 API 层做（422），存储层只归一化。"""
        caps = sorted(capabilities or [])
        if track in LEGACY_DOMAIN_MAP:
            track, mapped_caps = LEGACY_DOMAIN_MAP[track]
            caps = caps or mapped_caps
        # 自主配置（§6.8）：按轨补默认档（ctf=L0/assessment=L1/research=L1），非法值拒建
        cfg = dict(config or {})
        cfg["autonomy"] = normalize_autonomy(cfg.get("autonomy"), track=track)
        config = cfg
        slug = self._unique_slug(slugify(name, track))
        path = self.root / slug
        for sub in SUBDIRS:
            (path / sub).mkdir(parents=True)
        project_id = f"proj-{uuid.uuid4().hex[:12]}"
        meta = {
            "id": project_id,
            "name": name,
            "slug": slug,
            "track": track,
            "capabilities": caps,
            "created_at": _now(),
            "config": config or {},
        }
        bound = sorted({str(e).strip() for e in (experts or []) if str(e).strip()})
        if bound:
            meta["experts"] = bound
        (path / PROJECT_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        proj = Project(path)
        bb = proj.bb  # 建 db
        bb.create_project(name, track, caps, config, project_id=project_id)
        return proj

    def _unique_slug(self, base: str) -> str:
        slug, n = base, 2
        while (self.root / slug).exists():
            slug = f"{base}-{n}"
            n += 1
        return slug

    # ---------- 查询 / 打开 ----------

    def list_projects(self) -> list[dict]:
        """扫描 project.json（忽略 .trash）。损坏/缺文件的目录跳过（不抛错，列表页要稳）。
        排序按 created_at 倒序（新的在前）；meta 缺 created_at 的存量项目垫底，
        同值时间戳按目录名兜底（保证稳定序）。"""
        out = []
        for d in self.root.iterdir():
            if not d.is_dir() or d.name == TRASH_DIR:
                continue
            f = d / PROJECT_FILE
            if not f.is_file():
                continue
            try:
                out.append(view_meta(json.loads(f.read_text(encoding="utf-8"))))
            except (ValueError, OSError):
                continue
        out.sort(key=lambda m: (str(m.get("created_at") or ""), str(m.get("slug") or "")),
                 reverse=True)
        return out

    def open_project(self, key: str) -> Project:
        """按 slug 或项目 id 打开。找不到抛 FileNotFoundError。"""
        return Project(self._find_dir(key))

    # ---------- 配置（project.json + 黑板 projects 行双写） ----------

    def update_config(self, key: str, patch: dict, *,
                      project: Project | None = None) -> dict:
        """PATCH 项目 config：顶层键浅合并；autonomy 段整体替换并按轨归一化校验
        （非法值 ValueError→API 422）。project.json 与黑板 projects 行同事务语义
        双写对齐 create_project；传入内存句柄时同步其 meta（即时生效不靠重启）。"""
        p = project or self.open_project(key)
        meta = json.loads((p.path / PROJECT_FILE).read_text(encoding="utf-8"))
        track = project_binding(meta)[0]
        new_config = {**(meta.get("config") or {}), **(patch or {})}
        if "autonomy" in (patch or {}):
            new_config["autonomy"] = normalize_autonomy(patch["autonomy"], track=track)
        else:
            new_config["autonomy"] = normalize_autonomy(
                new_config.get("autonomy"), track=track)
        # 轨级语义（R2，§6.9 mode 退役 2026-09-17）：mission/redteam_roe 按轨归一化，
        # mode 键退役剥离（盘上旧值清除）；ROE 不再强制 422（缺省按 pentest 上限兜底）
        new_config.pop("mode", None)
        if "mission" in new_config or "redteam_roe" in new_config:
            from core.autonomy import normalize_track_semantics
            new_config.update(normalize_track_semantics(new_config, track=track))
        # F11 评级生效档案：归一化（非法 ValueError→422）；结果 {} 则剥键恢复缺省态
        if "rule_profiles" in (patch or {}):
            from core.autonomy import normalize_rule_profiles
            profiles = normalize_rule_profiles(patch["rule_profiles"])
            if profiles:
                new_config["rule_profiles"] = profiles
            else:
                new_config.pop("rule_profiles", None)
        # D10 策略顾问段（2026-09-24）：整段替换+归一化；结果 {} 剥键恢复代码缺省
        if "advisor" in (patch or {}):
            from core.autonomy import normalize_advisor
            advisor = normalize_advisor(patch["advisor"])
            if advisor:
                new_config["advisor"] = advisor
            else:
                new_config.pop("advisor", None)
        # 项目 executor 模型覆写（TRAE 新壳 M3，2026-09-25）：整段替换；
        # None/{} 剥键恢复跟随路由缺省（API 层先校验供应商/模型可构建再落盘）
        if "executor_llm" in (patch or {}):
            ov = patch["executor_llm"]
            if isinstance(ov, dict) and str(ov.get("provider") or "").strip():
                new_config["executor_llm"] = {
                    "provider": str(ov["provider"]).strip(),
                    "model": str(ov.get("model") or "").strip()}
            else:
                new_config.pop("executor_llm", None)
        meta["config"] = new_config
        (p.path / PROJECT_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        p.bb.update_project_config(meta["id"], new_config)
        p.meta = meta
        return view_meta(meta)

    def update_experts(self, key: str, experts: list[str] | None, *,
                       project: Project | None = None) -> dict:
        """换将（expert-pool M2，§4.4）：重写 meta["experts"] 并即时生效于
        下轮会话构造（构造链每次实时读 meta，无需重启）。空清单剥键 = 恢复
        存量直通态（caps_effective 走 meta.capabilities）。专家存在性/轨校验
        在 API 层做（422）；黑板 projects 行无此列，单一真相源 project.json。"""
        p = project or self.open_project(key)
        meta = json.loads((p.path / PROJECT_FILE).read_text(encoding="utf-8"))
        bound = sorted({str(e).strip() for e in (experts or []) if str(e).strip()})
        if bound:
            meta["experts"] = bound
        else:
            meta.pop("experts", None)
        (p.path / PROJECT_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        p.meta = meta
        return view_meta(meta)

    # ---------- 对话化编排器 meta（M2/M3，§6.4；单一真相源 project.json） ----------

    def update_phase_goal(self, key: str, goal: dict | None, *,
                          project: Project | None = None) -> dict:
        """阶段目标（对话化编排器 M2）：整键写 meta["phase_goal"]；None=清空重议
        （剥键）。goal 确认/清空的事件留痕（goal.confirm/goal.clear）由 API 层落
        （payload 带全文快照，变更历史=事件流可回放）；黑板 projects 行无此列。"""
        p = project or self.open_project(key)
        meta = json.loads((p.path / PROJECT_FILE).read_text(encoding="utf-8"))
        if goal:
            meta["phase_goal"] = goal
        else:
            meta.pop("phase_goal", None)
        (p.path / PROJECT_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        p.meta = meta
        return view_meta(meta)

    def update_orchestrator_persona(self, key: str, persona: dict | None, *,
                                    project: Project | None = None) -> dict:
        """编排器拟人身份（M3）：meta["orchestrator_persona"]={display_name, persona}
        整键写；None=恢复缺省（剥键）。persona 只注入对话轮系统提示，display_name
        由前端贯穿页签/气泡。"""
        p = project or self.open_project(key)
        meta = json.loads((p.path / PROJECT_FILE).read_text(encoding="utf-8"))
        if persona:
            meta["orchestrator_persona"] = persona
        else:
            meta.pop("orchestrator_persona", None)
        (p.path / PROJECT_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        p.meta = meta
        return view_meta(meta)

    # ---------- 知识继承（expert-pool M4b，§4.8） ----------

    def inherit_knowledge(self, src_key: str, dst: Project) -> dict:
        """建项目时从源项目继承知识（同 binary_sha256 语义 = 样本资产本体 +
        其 func_kb 行 + 蓝图）。方向定稿（§4.8）：**只新增不覆盖**（目标已有
        同键条目一律跳过）、**源项目只读**（样本文件 shutil.copy2，绝不移动/
        改写源盘）。黑板写走各自 bb 方法（单一写入口）；物理样本文件复制在
        本层（store 只管 DB 行）。临时打开的源句柄 finally 关闭（Windows 句柄
        不落残留）。返回统计 {binaries, func_kb, blueprints, skipped}。"""
        src = self.open_project(src_key)
        if src.id == dst.id:
            src.close()
            raise ValueError("知识继承源不能是目标项目自身")
        stats = {"binaries": 0, "func_kb": 0, "blueprints": 0, "skipped": 0}
        try:
            src_bb, dst_bb = src.bb, dst.bb
            # 1) binary 资产（sha256 存 value 列）+ samples/ 文件复制
            for a in src_bb.list_assets(src.id):
                if a.get("type") != "binary":
                    continue
                sha = a.get("value") or ""
                if dst_bb.find_asset(dst.id, "binary", sha) is not None:
                    stats["skipped"] += 1
                    continue
                meta = dict(a.get("meta") or {})
                rel = str(meta.get("path") or "")
                safe = rel and not rel.startswith(("/", "\\")) and ".." not in Path(rel).parts
                sfile = src.samples_dir / rel if safe else None
                if sfile is not None and sfile.is_file():
                    dpath = dst.samples_dir / rel
                    dpath.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sfile, dpath)
                else:
                    meta.pop("path", None)  # 源文件缺失：只继承资产行与知识行
                meta["inherited_from"] = src.id
                dst_bb.upsert_asset(dst.id, "binary", sha, meta=meta,
                                    author=a.get("author") or "system")
                stats["binaries"] += 1
            # 2) func_kb 行（analysis 笔记/风险标签/演变史随行）
            for f in src_bb.list_funcs(src.id):
                if dst_bb.lookup_func(dst.id, f["binary_sha256"], f["address"]):
                    stats["skipped"] += 1
                    continue
                dst_bb.upsert_func(dst.id, f["binary_sha256"], f["address"],
                                   f.get("name") or "", f.get("analysis") or "",
                                   f.get("risk_tags") or [], f.get("confidence", 0.5),
                                   analyzed_by=f.get("analyzed_by") or "inherit")
                stats["func_kb"] += 1
            # 3) 蓝图（modules/content_md 随行；状态尽力保留，不可跳级则留 draft）
            existing = {b["name"] for b in dst_bb.list_blueprints(dst.id)}
            for b in src_bb.list_blueprints(src.id):
                if b["name"] in existing:
                    stats["skipped"] += 1
                    continue
                row = dst_bb.create_blueprint(
                    dst.id, b["name"], goal=b.get("goal") or "",
                    binary_sha256=b.get("binary_sha256") or "",
                    modules=b.get("modules") or [], content_md=b.get("content_md") or "",
                    author="inherit")
                if b.get("status") and b["status"] != "draft":
                    try:
                        dst_bb.set_blueprint_status(dst.id, row["id"], b["status"],
                                                    author="inherit")
                    except ValueError:
                        pass
                stats["blueprints"] += 1
        finally:
            src.close()
        return stats

    # ---------- 删除（回收站式） ----------

    def delete_project(self, key: str, *, project: Project | None = None) -> Path:
        """把项目目录整体 rename 到 .trash/<slug>-<UTC时间戳>/，返回新路径。
        不物理删除；恢复 = 手动把 .trash/<目录>/ 移回 workspaces/<slug>/。
        传入 project 句柄时先关其全部黑板连接（Windows 下 db 被占用 rename
        会抛 PermissionError）；未传句柄时调用方须自行保证无连接残留。"""
        if project is not None:
            project.bb.close_all()
            project.close()
            d = project.path
        else:
            d = self._find_dir(key)
        trash = self.root / TRASH_DIR
        trash.mkdir(exist_ok=True)  # 惰性创建：不删项目就不产生 .trash
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dst = trash / f"{d.name}-{ts}"
        n = 2
        while dst.exists():  # 同秒重删同名 → <slug>-<ts>-2
            dst = trash / f"{d.name}-{ts}-{n}"
            n += 1
        # Windows 上 sqlite 句柄关闭到目录可 rename 之间可能有毫秒级延迟
        # （杀软扫描/句柄延迟释放），短退避重试吸收；耗尽后由 API 层转 422。
        # 不在重试里再碰 project.bb：close() 后该 property 会新建 Blackboard 实例，
        # 且 close_all 的关闭闸门已保证 _conns 不会再增长。
        last_perr: PermissionError | None = None
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                d.rename(dst)
                return dst
            except PermissionError as e:
                last_perr = e
        raise last_perr  # type: ignore[misc]

    def list_trashed(self) -> list[dict]:
        """扫描 .trash/*/project.json（手动恢复时确认里面有什么用）。"""
        out = []
        trash = self.root / TRASH_DIR
        if not trash.is_dir():
            return out
        for d in sorted(trash.iterdir()):
            f = d / PROJECT_FILE
            if not d.is_dir() or not f.is_file():
                continue
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        return out

    # ---------- 内部 ----------

    def _find_dir(self, key: str) -> Path:
        """按 slug 或项目 id 定位项目目录（忽略 .trash）。找不到抛 FileNotFoundError。"""
        for d in self.root.iterdir():
            if not d.is_dir() or d.name == TRASH_DIR:
                continue
            f = d / PROJECT_FILE
            if not f.is_file():
                continue
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if key in (meta.get("slug"), meta.get("id")):
                return d
        raise FileNotFoundError(f"项目不存在: {key}（root={self.root}）")
