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

import json
import re
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
        """启用能力包（多选）。"""
        return project_binding(self.meta)[1]

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
    ) -> Project:
        """创建项目目录与黑板库。id 全局唯一（project.json 与 projects 行同 id）。

        v2 绑定：track（场景轨单选）+ capabilities（能力包多选）。
        旧域名（pentest/ctf）作 track 传入时透明映射（读兼容、写新值，§4.5.5）。"""
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
        """扫描 project.json（忽略 .trash）。损坏/缺文件的目录跳过（不抛错，列表页要稳）。"""
        out = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name == TRASH_DIR:
                continue
            f = d / PROJECT_FILE
            if not f.is_file():
                continue
            try:
                out.append(view_meta(json.loads(f.read_text(encoding="utf-8"))))
            except (ValueError, OSError):
                continue
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
        # C2 作战模式（§6.9）：mode/mission/redteam_roe 归一化；mode 翻转时审计
        old_mode = (meta.get("config") or {}).get("mode", "pentest")
        if any(k in (patch or {}) for k in ("mode", "mission", "redteam_roe")):
            from core.autonomy import normalize_mode_config
            merged_mode = normalize_mode_config(new_config)
            new_config.update(merged_mode)
            if merged_mode["mode"] != old_mode:
                meta["config"] = new_config
                (p.path / PROJECT_FILE).write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                p.bb.append_event(
                    meta["id"], "mode.changed",
                    {"old": old_mode, "new": merged_mode["mode"],
                     "roe": merged_mode.get("redteam_roe")},
                    author="human")
        meta["config"] = new_config
        (p.path / PROJECT_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        p.bb.update_project_config(meta["id"], new_config)
        p.meta = meta
        return view_meta(meta)

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
