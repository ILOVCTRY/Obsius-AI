"""项目工作区管理（DESIGN.md §5.3 定稿布局）。

项目 = workspaces/<slug>/ 一个文件夹：
  project.json（元数据）+ blackboard.db（本项目全部黑板数据）
  + samples/（人放的 untrusted 输入）+ artifacts/（Agent 产物）+ logs/（可选）

原则：
- 每项目一个 SQLite 库；归档/备份 = 操作文件夹。
- 删除 = 回收站式（DESIGN.md §5.3）：整目录 rename 进 workspaces/.trash/，
  不物理删除；恢复 = restore_project()（前端回收站一键，同名自动加后缀）；
  所有扫描忽略 .trash。
- 回收站终点操作（2026-10-18）：purge_trashed() 物理抹除整条目（永久删除）；
  clear_trashed_contents() 只清除条目内项目文件、保留 project.json 墓碑
  （名字信息仍在清单里，元数据落 trash_purged_at，此后不可恢复）。
- 不建全局注册表：项目列表 = 扫描 workspaces/*/project.json（单一真相源）。
- id 由本模块统一生成，blackboard projects 行同 id。
"""

import fnmatch
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.autonomy import normalize_autonomy
from core.blackboard.store import Blackboard, BlackboardClosedError
from core.skills.taxonomy import LEGACY_DOMAIN_MAP, project_binding

log = logging.getLogger(__name__)

PROJECT_FILE = "project.json"
DB_FILE = "blackboard.db"
SUBDIRS = ("samples", "artifacts", "logs")
TRASH_DIR = ".trash"
WORKSPACE_TREE_MAX_DEPTH = 4
WORKSPACE_TREE_MAX_NODES = 1000
WORKSPACE_TREE_MAX_CHILDREN = 200
_WORKSPACE_HIDDEN_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules",
                          "__pycache__", ".history", ".trash", ".tmp",
                          "trees", "raw"}
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


# Windows 文件属性位（os.lstat().st_file_attributes）：判定**只看 lstat、绝不
# follow 链接**。Agent 经 WSL/容器在项目里建的 venv 等会留下 WSL LX symlink
# （IO_REPARSE_TAG_LX_SYMLINK）：Windows 原生 Python 的 os.path.islink() 不认
# （False），而 os.stat()/is_dir() **会 follow** → 抛 WinError 1920
# （ERROR_CANT_ACCESS_FILE）。shutil.rmtree 正是死在「is_dir() 1920」且异常不
# 是 PermissionError/FileNotFoundError——曾把回收站永久删除放大成 500。
#
# 但「不 follow 分类」只是第一层：**LX 链接本体在 Windows 上删不掉**。实测
# （2026-10-19，真机 E: 盘）对 attrs=0x400/0x410 的 LX 链接：
#   os.unlink / DeleteFileW / RemoveDirectoryW / SetFileInformationByHandle
#   (FileDispositionInfo|Ex, 含 POSIX 语义) / NtSetInformationFile /
#   shutil.rmtree / cmd del / PowerShell Remove-Item / robocopy /MIR / MSYS rm
#   → 一律 WinError 5 (ERROR_ACCESS_DENIED)；而 os.rename 同路径却成功。
# 唯一可行路径：以 DELETE|FILE_WRITE_ATTRIBUTES 打开并
# FSCTL_DELETE_REPARSE_POINT **先摘掉重解析标签**，之后它退化为普通文件/空目录，
# 再走常规删除即成功（见 _strip_reparse_point / _remove_link）。
#
# 另一独立坑：WSL 程序写的文件常带 READONLY 位（如 X11 的 .X99-lock，
# attrs=0x21）。Windows 上 os.unlink 对只读文件同样 WinError 5，需先清位。
_FILE_ATTR_READONLY = 0x1
_FILE_ATTR_REPARSE_POINT = 0x400
_FILE_ATTR_DIRECTORY = 0x10
_FILE_ATTR_NORMAL = 0x80

# FSCTL（reparse point 读取/摘除）
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_FSCTL_DELETE_REPARSE_POINT = 0x000900AC
_FILE_WRITE_ATTRIBUTES = 0x0100
_GENERIC_READ = 0x80000000
_DELETE = 0x00010000
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


def _win_reparse_handle(path: Path, access: int):
    r"""以「不 follow 链接」方式打开 reparse point，返回 Win32 HANDLE。

    非 Windows / 打不开返回 None。用 ``\\?\`` 前缀避开 MAX_PATH 与 8.3 名解析。
    """
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.CreateFileW.restype = wt.HANDLE
    ap = os.path.abspath(path)
    if not ap.startswith("\\\\?\\"):
        ap = "\\\\?\\" + ap
    h = k32.CreateFileW(ap, access, _SHARE_ALL, None, _OPEN_EXISTING,
                        _FILE_FLAG_OPEN_REPARSE_POINT
                        | _FILE_FLAG_BACKUP_SEMANTICS, None)
    if h == wt.HANDLE(-1).value:
        return None
    return k32, h


def _strip_reparse_point(path: Path) -> bool:
    """摘掉 reparse 标签（symlink/junction/WSL LX 链），使条目退化为普通
    文件/空目录——这是 Windows 上删除 LX 链接的**唯一**可行路径。

    成功返回 True；非 Windows、不是 reparse point、或无权限（打不开/FSCTL
    被拒）返回 False，由调用方继续按常规方式尝试。"""
    if os.name != "nt":
        return False
    import ctypes
    import ctypes.wintypes as wt

    opened = _win_reparse_handle(path, _DELETE | _FILE_WRITE_ATTRIBUTES)
    if opened is None:
        return False
    k32, h = opened
    try:
        # 先读原 tag：FSCTL_DELETE_REPARSE_POINT 的入参必须带正确 ReparseTag，
        # 否则返回 ERROR_REPARSE_TAG_MISMATCH。
        buf = ctypes.create_string_buffer(16 * 1024)
        ret = wt.DWORD(0)
        if not k32.DeviceIoControl(h, _FSCTL_GET_REPARSE_POINT, None, 0, buf,
                                   len(buf), ctypes.byref(ret), None):
            return False
        tag = ctypes.c_uint32.from_buffer_copy(buf.raw[:4]).value
        # REPARSE_DATA_BUFFER：ReparseTag(4) + ReparseDataLength(2) + Reserved(2)
        inbuf = ctypes.create_string_buffer(8)
        ctypes.memmove(inbuf, ctypes.byref(ctypes.c_uint32(tag)), 4)
        ret2 = wt.DWORD(0)
        if not k32.DeviceIoControl(h, _FSCTL_DELETE_REPARSE_POINT, inbuf, 8,
                                   None, 0, ctypes.byref(ret2), None):
            return False
        return True
    finally:
        k32.CloseHandle(h)


def _clear_readonly(path: Path) -> None:
    """清 Windows 只读位（WSL 程序留下的 .X99-lock 等常带 0x1）——
    os.unlink 对只读文件抛 WinError 5。失败静默（交给后续删除报真因）。"""
    if os.name != "nt":
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        return
    try:
        st = os.lstat(path)
        if getattr(st, "st_file_attributes", 0) & _FILE_ATTR_READONLY:
            os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


def _fs_kind(st: os.stat_result) -> str:
    """lstat 结果分类为 "link" / "dir" / "file"（判定过程绝不 follow 链接）。

    Windows 用属性位（reparse point=链接，含 symlink/junction/WSL LX）；
    POSIX 用 S_ISLNK/S_ISDIR。二者都不解析链接目标，LX 链接也能安全判定。"""
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is None:
        if stat.S_ISLNK(st.st_mode):
            return "link"
        return "dir" if stat.S_ISDIR(st.st_mode) else "file"
    if attrs & _FILE_ATTR_REPARSE_POINT:
        return "link"
    return "dir" if attrs & _FILE_ATTR_DIRECTORY else "file"


def _du_bytes(path: Path) -> int:
    """文件/目录占用字节（仅统计；symlink/LX 链接不 follow 按 0 体积计，
    读取失败跳过）。同 _fs_kind 口径，避免 rglob 撞 LX 链接整体归零。"""
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        kind = _fs_kind(st)
        if kind == "dir":
            try:
                with os.scandir(p) as it:
                    stack.extend(p / e.name for e in it)
            except OSError:
                continue
        elif kind == "file":
            total += st.st_size
    return total


def _unlink_retry(path: Path) -> None:
    """删文件/链接（含 reparse point 链接本体）；Windows 句柄延迟退避重试，
    已不存在视为成功，耗尽抛 PermissionError。

    只读位在 Windows 上同样导致 WinError 5（WSL 程序写的 .X99-lock 等），
    故首轮失败即清位再试。"""
    last_perr: PermissionError | None = None
    cleared = False
    for delay in (0.0, 0.05, 0.1, 0.2):
        if delay:
            time.sleep(delay)
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as e:
            last_perr = e
            if not cleared:
                _clear_readonly(path)
                cleared = True
    raise last_perr  # type: ignore[misc]


def _rmdir_retry(path: Path) -> None:
    """删空目录（目录型 reparse point 的 unlink 被拒时退此路）；语义同
    _unlink_retry。"""
    last_perr: PermissionError | None = None
    cleared = False
    for delay in (0.0, 0.05, 0.1, 0.2):
        if delay:
            time.sleep(delay)
        try:
            path.rmdir()
            return
        except FileNotFoundError:
            return
        except PermissionError as e:
            last_perr = e
            if not cleared:
                _clear_readonly(path)
                cleared = True
    raise last_perr  # type: ignore[misc]


def _remove_link(path: Path) -> None:
    """删「链接本体」（symlink/junction/WSL LX 链接），绝不触碰链接目标。

    顺序（每步失败才进下一步，任一步成功即返回）：
      1. unlink —— 普通 symlink / 文件型 junction 走这条；
      2. rmdir  —— 目录型 reparse（junction）RemoveDirectory 可删；
      3. 摘 reparse 标签再删 —— **WSL LX 链接的唯一出路**：上面两步在 LX 链上
         一律 WinError 5（实测见模块头注释），FSCTL_DELETE_REPARSE_POINT 摘掉
         标签后退化为普通空文件/空目录，常规删除即可。"""
    try:
        _unlink_retry(path)
        return
    except OSError:
        pass
    try:
        _rmdir_retry(path)
        return
    except OSError:
        pass
    if _strip_reparse_point(path):
        st = os.lstat(path)
        if _fs_kind(st) == "dir":
            _rmdir_retry(path)
        else:
            _unlink_retry(path)
        return
    # 三条路都失败：把最后一次的真实异常抛出去（调用方聚合/转 422）
    st = os.lstat(path)
    if _fs_kind(st) == "dir":
        _rmdir_retry(path)
    else:
        _unlink_retry(path)


def _remove_entry(path: Path) -> None:
    """递归删除文件/目录，兼容 Windows reparse point（symlink/junction/WSL LX
    链接）：_fs_kind 属性位判定、绝不 follow——目录型链接只删链接本体，不会
    把链接目标内容牵连进来。逐项退避重试；有残留时 PermissionError（占用）
    原类抛出、其他 OSError 聚合抛出（API 层转 422，不再漏成 500）。"""
    errs: list[OSError] = []

    def walk(p: Path) -> None:
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return
        except OSError as e:
            errs.append(e)
            return
        kind = _fs_kind(st)
        if kind == "link":
            try:
                _remove_link(p)
            except OSError as e:
                errs.append(e)
            return
        if kind == "file":
            try:
                _unlink_retry(p)
            except OSError as e:
                errs.append(e)
            return
        try:
            with os.scandir(p) as it:
                names = [e.name for e in it]  # 只取名不做 stat，避开 LX 子链
        except FileNotFoundError:
            return
        except OSError as e:
            errs.append(e)
            return
        for name in names:
            walk(p / name)
        try:
            _rmdir_retry(p)
        except OSError as e:
            errs.append(e)

    walk(path)
    if errs:
        detail = "; ".join(f"{e.filename or path}: {e}" for e in errs[:5])
        first = errs[0]
        msg = f"删除残留 {len(errs)} 项: {detail}"
        if isinstance(first, PermissionError):
            raise PermissionError(msg) from first
        raise OSError(msg) from first


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
        """盘上原始能力/方向绑定（读兼容字段，能力包已退役）。专家绑定项目的运行时
        可见范围用 directions_effective 推导（app 层），本属性恒返回盘上原始绑定值
        （无绑定/未落键 = 空表），供 directions_effective 的 fallback 与存量直通使用。"""
        return list(self.meta.get("capabilities") or [])

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
            # feishu-push v1：新开黑板即挂飞书推送中枢（幂等按 pid；
            # 连接器 disabled/未装配 = 零副作用；装配失败绝不阻断黑板）
            try:
                from core.integrations.hub import get_default_hub
                get_default_hub().attach_store(self.id, self._bb,
                                               project_name=self.name)
            except Exception:  # noqa: BLE001
                log.exception("飞书推送中枢挂载失败（不影响黑板）: %s", self.id)
        return self._bb

    @property
    def samples_dir(self) -> Path:
        return self.path / "samples"

    @property
    def artifacts_dir(self) -> Path:
        return self.path / "artifacts"

    def close(self) -> None:
        if self._bb is not None:
            try:  # feishu-push v1：退订释放（与 attach 对称）
                from core.integrations.hub import get_default_hub
                get_default_hub().detach(self.id)
            except Exception:  # noqa: BLE001
                log.debug("飞书推送退订失败: %s", self.id)
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
        # 知识库迭代开关（kb-iteration，2026-10-14）：整段替换+归一化（非法 422）；
        # 缺省补 {enabled: False}（与 autonomy 同款，读路径无段也走默认）
        from core.skills.kbiteration import normalize_kb_iteration
        new_config["kb_iteration"] = normalize_kb_iteration(
            (patch or {}).get("kb_iteration", new_config.get("kb_iteration")))
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
        # CTF 赛制时间（ctf-pacing，2026-10-19）：整段替换+归一化（非法 422）；
        # 结果 {} 剥键 → 读路径回落阶梯推断。此前只能手改 project.json。
        if "ctf" in (patch or {}):
            from core.ctf.pacing import normalize_ctf_config
            ctf = normalize_ctf_config(patch["ctf"])
            if ctf:
                new_config["ctf"] = ctf
            else:
                new_config.pop("ctf", None)
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
        # 调用方可能持有项目缓存；同步内存元数据，避免保存后立即读取旧 persona。
        p.meta = meta
        (p.path / PROJECT_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
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
        """扫描 .trash/*/project.json（回收站清单；恢复时确认里面有什么用）。

        每项补 ``trash_name`` = 回收站目录名（``<slug>-<UTC时间戳>``）——它既是
        恢复操作的稳定句柄（目录名会被重命名，项目 id/name 反而可能与新项目重名），
        也是删除时间的展示来源。"""
        out = []
        trash = self.root / TRASH_DIR
        if not trash.is_dir():
            return out
        for d in sorted(trash.iterdir()):
            f = d / PROJECT_FILE
            if not d.is_dir() or not f.is_file():
                continue
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            meta["trash_name"] = d.name
            out.append(meta)
        return out

    def restore_project(self, trash_name: str) -> tuple[Path, str]:
        """把 .trash/<trash_name>/ 移回 workspaces/<slug>/（回收站恢复）。

        目标 slug 已被占用（同项目名已存在）时自动加 ``-2``/``-3`` 后缀——项目
        name/id 不变，仅目录名让位，故恢复永不因重名失败。返回
        ``(新项目目录, 实际 slug)``；找不到抛 FileNotFoundError，非法句柄/已清除
        文件的墓碑条目抛 ValueError（防路径穿越：不接受分隔符/上跳）。
        """
        src = self._trash_src(trash_name)
        meta_file = src / PROJECT_FILE
        if not meta_file.is_file():
            raise FileNotFoundError(f"回收站条目不存在: {trash_name}")
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise FileNotFoundError(f"回收站条目元数据损坏: {trash_name}") from e
        if not isinstance(meta, dict):
            raise FileNotFoundError(f"回收站条目元数据损坏: {trash_name}")
        if meta.get("trash_purged_at"):
            # 墓碑条目：clear_trashed_contents 已抹掉黑板库等数据，恢复只会
            # 得到残缺项目（无 blackboard.db），明确拒绝（前端同口径禁用）。
            raise ValueError(
                f"回收站条目已清除项目文件（墓碑），无法恢复: {trash_name}")
        slug = self._unique_slug(
            slugify(str(meta.get("name") or trash_name), str(meta.get("track") or "ctf")))
        dst = self.root / slug
        # 与 delete_project 同款退避：Windows 上句柄关闭到目录可 rename 间有毫秒级延迟
        last_perr: PermissionError | None = None
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                src.rename(dst)
                break
            except PermissionError as e:
                last_perr = e
        else:
            raise last_perr  # type: ignore[misc]
        # slug 冲突让位（-2/-3）后目录名 ≠ meta.slug，会让 _find_dir 同值歧义——
        # 就地改写 meta.slug 对齐新目录名（黑板行无 slug 列，仅 project.json）。
        if meta.get("slug") != slug:
            meta["slug"] = slug
            (dst / PROJECT_FILE).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return dst, slug

    # ---------- 回收站终点：永久删除 / 清除项目内文件（2026-10-18） ----------

    def purge_trashed(self, trash_name: str) -> Path:
        """永久删除回收站条目（物理抹除，不可恢复）：rmtree .trash/<trash_name>/。

        回收站链路的终点，与 delete_project「整目录移入回收站」相对。返回被
        抹除的目录路径（仅作回执，调用后已不存在）。条目不存在 FileNotFoundError；
        非法句柄 ValueError；删除失败抛 OSError/PermissionError（API 转 422，
        含 reparse point 兼容——见 _remove_entry）。"""
        src = self._trash_src(trash_name)
        # _remove_entry 兼容 WSL LX symlink 等 reparse point（旧实现 shutil.rmtree
        # 会在 Windows 上抛非 Permission 的 WinError 1920，漏成 500）。
        _remove_entry(src)
        return src

    def clear_trashed_contents(self, trash_name: str) -> dict:
        """清除回收站条目内的项目文件，仅保留 project.json（墓碑，不可逆）。

        黑板库/样本/产物/日志等物理抹除，但条目仍留在回收站清单里
        （list_trashed 认 project.json）——名字/轨道/专家绑定继续可展示；
        meta 落 ``trash_purged_at`` 墓碑戳，前端据此禁用「恢复」，
        该条目只剩「永久删除」收口。找不到/元数据损坏 FileNotFoundError；
        非法句柄 ValueError；删除失败 OSError/PermissionError（API 转 422，
        兼容 reparse point——见 _remove_entry）。"""
        src = self._trash_src(trash_name)
        meta_file = src / PROJECT_FILE
        if not meta_file.is_file():
            raise FileNotFoundError(f"回收站条目元数据损坏: {trash_name}")
        freed = removed = 0
        for entry in sorted(src.iterdir()):
            if entry.name == PROJECT_FILE:
                continue
            freed += _du_bytes(entry)
            removed += 1
            _remove_entry(entry)
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["trash_purged_at"] = _now()
        meta_file.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"trash_name": trash_name, "removed_entries": removed,
                "freed_bytes": freed, "kept": [PROJECT_FILE],
                "purged_at": meta["trash_purged_at"]}

    def _trash_src(self, trash_name: str) -> Path:
        """回收站句柄 → .trash/<trash_name> 目录。非法句柄 ValueError（与 packs
        校验同口径的防穿越：不接受分隔符/上跳/空）；条目不存在 FileNotFoundError。"""
        if (not trash_name or "/" in trash_name or "\\" in trash_name
                or trash_name in (".", "..") or ".." in Path(trash_name).parts):
            raise ValueError(f"非法回收站句柄: {trash_name!r}")
        src = self.root / TRASH_DIR / trash_name
        if not src.is_dir():
            raise FileNotFoundError(f"回收站条目不存在: {trash_name}")
        return src

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
