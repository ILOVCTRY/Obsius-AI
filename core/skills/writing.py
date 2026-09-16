"""packs/ 受控写入（DESIGN.md §4：唯一写 packs 的地方）。

两层原语：

1. **进程写锁**：``pack_write_lock``（RLock）。设置页所有 packs 文件写（技能/角色/
   红线/owners/MCP/历史备份/trash/回滚/kb/提案应用）串行化，消除并发保存的丢失更新。
   RLock 让端点临界区内可再调备份/trash 辅助。只覆盖单进程 uvicorn（与 app.state
   会话表一致）；多进程部署需换文件锁/Postgres，当前明确不支持。

2. **kb 本地基线写入（C 批）**：知识快照从"只读文物"翻为"可增改的本地基线"
   （DESIGN §4，2026-09-14）。纪律由调用方与校验共同保证：英文快照原文不翻译、
   新增经验写新 md（rename/delete 走引用联动，见 core/skills/refs.py）。

所有 mutation 函数内部取锁；纯校验/解析函数不取锁。
"""

import json
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from core.skills.rules import KbSource, load_kb_sources
from core.skills.taxonomy import capability_dir, track_dir

_PACK_WRITE_LOCK = threading.RLock()

# kb 文件硬上限（1 MiB：方法论文档足够，防误传大文件进 packs）
KB_MAX_BYTES = 1 << 20
# Windows 非法文件名字符（/ 是分隔符不算）；其余中文/字母数字/-_.() 空格允许
_KB_FORBIDDEN_CHARS = set('<>:"|?*\x00')
_KB_NAME_RE = re.compile(r"^[\w][\w .()-]{0,127}$")
_UTC_TS_FMT = "%Y%m%dT%H%M%SZ"


class KbError(ValueError):
    """kb 路径/内容非法（API 层映射 422）。"""


@contextmanager
def pack_write_lock():
    """packs/config 受控文件写的临界区（进程内单写者，可重入）。"""
    with _PACK_WRITE_LOCK:
        yield


# ---------- 通用受控文件原语（技能/角色/规则/owners/mcp） ----------

def _utc_ts() -> str:
    return time.strftime(_UTC_TS_FMT, time.gmtime())


def backup_history(path: Path) -> Path | None:
    """写入前留修改历史：同级 ``.history/<UTC时间戳>[.n]_<文件名>``。

    与设置页历史端点（回滚/diff）共用命名；同秒多次写入用 .n 避让，绝不覆盖既有版本。
    原文件不存在则无备份（返回 None）。调用方须已持锁（或在 pack_write_lock 内）。"""
    if not path.is_file():
        return None
    hist = path.parent / ".history"
    hist.mkdir(parents=True, exist_ok=True)
    ts = _utc_ts()
    dest = hist / f"{ts}_{path.name}"
    n = 1
    while dest.exists():
        dest = hist / f"{ts}.{n}_{path.name}"
        n += 1
    shutil.copy2(path, dest)
    return dest


def backup_and_write(path: Path, content: str) -> Path | None:
    """通用"备份→全文写"（技能/提案 skill 应用等）；返回备份路径（新建时为 None）。

    父目录自动创建。整段在写锁临界区内。"""
    with pack_write_lock():
        backup = backup_history(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return backup


def trash_move(src: Path) -> Path:
    """删除不物理抹除：移入同级 ``.history/trash/<名>.<UTC时间戳>[.n](.bak)``。

    同秒二次删除用序号避让。调用方须已持锁。"""
    trash = src.parent / ".history" / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    ts = _utc_ts()
    is_dir = src.is_dir()
    tail = "" if is_dir else ".bak"
    dest = trash / f"{src.name}.{ts}{tail}"
    n = 1
    while dest.exists():
        dest = trash / f"{src.name}.{ts}.{n}{tail}"
        n += 1
    shutil.move(str(src), str(dest))
    return dest


# ---------- kb 目标解析与校验 ----------

@dataclass
class KbTarget:
    cap: str
    source: KbSource
    rel: Path            # 源 root 内相对路径（posix 语义，可多级）
    path: Path           # resolve 后的绝对路径

    @property
    def module(self) -> str:
        return self.rel.as_posix()


def _validate_rel(rel_path: str) -> Path:
    """kb 模块相对路径校验：非空、非绝对、无 ..、限 .md、中文多级目录允许、
    拒绝 Windows 非法字符与空字节；不允许落到 .history（备份区永不接受编辑）。"""
    if not rel_path or not rel_path.strip():
        raise KbError("路径不能为空")
    p = Path(rel_path.strip().replace("\\", "/"))
    if p.is_absolute() or not p.parts or ".." in p.parts:
        raise KbError("必须是知识库内相对路径（禁止绝对路径 / .. 穿越）")
    if p.parts[0] == ".history":
        raise KbError(".history 是备份区，不可直接编辑/新建")
    for seg in p.parts:
        if not seg or seg in {".", ""} or any(c in seg for c in _KB_FORBIDDEN_CHARS):
            raise KbError(f"非法路径段: {seg!r}")
    if p.suffix.lower() != ".md":
        raise KbError("知识库只接受 .md 文件")
    if not _KB_NAME_RE.fullmatch(p.stem):
        raise KbError(f"文件名不合法: {p.name!r}")
    return p


def _sources(packs_root: str | Path, cap: str) -> list[KbSource]:
    sources = load_kb_sources(packs_root, [cap])
    if not sources:
        raise KbError(f"能力包 {cap} 未登记 kb_sources.json（无知识库源）")
    return sources


def resolve_kb(packs_root: str | Path, cap: str, rel_path: str) -> KbTarget:
    """把 (能力包, 源内相对模块路径) 解析为落盘目标。

    已存在文件按落点归属源；新文件默认落第一个源（现网均为单源 root=kb）。
    resolve+commonpath 双保险防穿越。"""
    rel = _validate_rel(rel_path)
    root = Path(packs_root)
    sources = _sources(root, cap)
    # 已存在文件按真实落点归属源（多源同名消歧靠存在性，不靠猜）
    for src in sources:
        candidate = (src.root / rel).resolve()
        if candidate.is_file() and src.root in candidate.parents:
            return KbTarget(cap=cap, source=src, rel=rel, path=candidate)
    # 新文件默认落登记的第一个源（现网均单源 root=kb）
    chosen = sources[0]
    resolved = (chosen.root / rel).resolve()
    try:
        if resolved != chosen.root and chosen.root not in resolved.parents:
            raise KbError("路径越出知识库根")
    except OSError as e:
        raise KbError(f"非法路径: {e}") from e
    return KbTarget(cap=cap, source=chosen, rel=rel, path=resolved)


def _validate_content(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise KbError("内容不能为空")
    if len(content.encode("utf-8")) > KB_MAX_BYTES:
        raise KbError(f"单文件超过 {KB_MAX_BYTES // 1024} KiB 上限")
    if "\x00" in content:
        raise KbError("内容含空字节")
    return content


# ---------- kb 列举/读 ----------

def list_kb(packs_root: str | Path, cap: str) -> list[dict]:
    """列某能力包知识库：每个源一棵树（只列 .md，排除 .history），按 posix 相对路径排序。"""
    out = []
    for src in _sources(Path(packs_root), cap):
        files: list[dict] = []
        if src.root.is_dir():
            iterator = src.root.rglob("*.md") if src.recursive else src.root.glob("*.md")
            for f in sorted(iterator):
                if ".history" in f.relative_to(src.root).parts:
                    continue
                st = f.stat()
                files.append({"path": f.relative_to(src.root).as_posix(),
                              "size": st.st_size,
                              "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(st.st_mtime))})
        out.append({"id": src.id, "recursive": src.recursive,
                    "root": src.root.name, "files": files})
    return out


# ---------- kb 正文搜索 ----------

# 单文件命中计数上限（排序够用，防超大文档刷爆计数）
_KB_SEARCH_COUNT_CAP = 20
# snippet 首处命中前后保留的字符数
_KB_SNIPPET_CHARS = 60


def search_kb(packs_root: str | Path, cap: str, q: str, limit: int = 50) -> list[dict]:
    """kb 正文搜索（只读，与 list_kb 同一源遍历口径）。

    大小写不敏感 substring；kb 文件量级为数百篇 × ≤1 MiB，全量遍历即可，不建索引。
    每命中文件回 {path, source, matches, snippet}；按命中次数降序、同分按路径，cap limit。
    空 q 返回空列表（交由调用方决定是否提示）。"""
    needle = (q or "").strip().lower()
    if not needle:
        return []
    out: list[dict] = []
    for src in _sources(Path(packs_root), cap):
        if not src.root.is_dir():
            continue
        iterator = src.root.rglob("*.md") if src.recursive else src.root.glob("*.md")
        for f in sorted(iterator):
            if ".history" in f.relative_to(src.root).parts:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits = text.lower().count(needle)
            if not hits:
                continue
            pos = text.lower().find(needle)
            start = max(0, pos - _KB_SNIPPET_CHARS)
            end = min(len(text), pos + len(needle) + _KB_SNIPPET_CHARS)
            out.append({"path": f.relative_to(src.root).as_posix(),
                        "source": src.id,
                        "matches": min(hits, _KB_SEARCH_COUNT_CAP),
                        "snippet": text[start:end].replace("\n", " ").strip()})
    out.sort(key=lambda r: (-r["matches"], r["path"]))
    return out[:max(1, limit)]


# ---------- kb 备份布局（kb/<root>/.history/kb-backups 与 kb-trash） ----------

def _kb_history_root(target: KbTarget) -> Path:
    return target.source.root / ".history"


def _flatten(rel: Path) -> str:
    return "__".join(rel.parts)


def _kb_backup_path(target: KbTarget) -> Path:
    base = _kb_history_root(target) / "kb-backups"
    flat = _flatten(target.rel)
    ts = _utc_ts()
    dest = base / f"{ts}__{flat}.bak"
    n = 1
    while dest.exists():
        dest = base / f"{ts}.{n}__{flat}.bak"
        n += 1
    return dest


def _kb_trash_path(target: KbTarget) -> Path:
    base = _kb_history_root(target) / "kb-trash"
    flat = _flatten(target.rel)
    ts = _utc_ts()
    dest = base / f"{flat}.{ts}.bak"
    n = 1
    while dest.exists():
        dest = base / f"{flat}.{ts}.{n}.bak"
        n += 1
    return dest


# ---------- kb 写/删/改名/版本 ----------

def write_kb_file(packs_root: str | Path, cap: str, rel_path: str, content: str,
                  *, create: bool) -> dict:
    """新建（POST，create=True，文件必须不存在）或全文改写（PUT，create=False，必须存在）。

    写前留备份到 kb/.history/kb-backups/。返回 {path, backup}。"""
    text = _validate_content(content)
    with pack_write_lock():
        target = resolve_kb(packs_root, cap, rel_path)
        exists = target.path.is_file()
        if create and exists:
            raise KbError(f"文件已存在: {target.module}（改写请用 PUT）")
        if not create and not exists:
            raise KbError(f"文件不存在: {target.module}（新建请用 POST）")
        backup = None
        if exists:
            dest = _kb_backup_path(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target.path, dest)
            backup = dest.name
        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_text(text, encoding="utf-8")
        return {"path": target.module, "backup": backup, "created": not exists}


def delete_kb_file(packs_root: str | Path, cap: str, rel_path: str) -> dict:
    """删除 kb 文件 → 移入 kb/.history/kb-trash/（可恢复，不物理抹除）。"""
    with pack_write_lock():
        target = resolve_kb(packs_root, cap, rel_path)
        if not target.path.is_file():
            raise KbError(f"文件不存在: {target.module}")
        dest = _kb_trash_path(target)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target.path), str(dest))
        return {"path": target.module, "trash": dest.name}


def rename_kb_move(packs_root: str | Path, cap: str, old_rel: str, new_rel: str) -> dict:
    """同源内移动 kb 文件（不备份文件自身——内容不变，回退=再改名；引用文件的备份
    与替换由 refs.rewrite_module 负责，见 C3）。新路径必须不存在。"""
    with pack_write_lock():
        old = resolve_kb(packs_root, cap, old_rel)
        new = resolve_kb(packs_root, cap, new_rel)
        if old.source.root != new.source.root:
            raise KbError("改名仅限同一知识库源内（跨源移动不支持）")
        if not old.path.is_file():
            raise KbError(f"文件不存在: {old.module}")
        if new.path.exists():
            raise KbError(f"目标已存在: {new.module}")
        new.path.parent.mkdir(parents=True, exist_ok=True)
        old.path.rename(new.path)
        return {"old_path": old.module, "new_path": new.module}


def kb_versions(packs_root: str | Path, cap: str, rel_path: str) -> list[dict]:
    """列某 kb 文件的历史版本（kb-backups 内，按时间倒序）。"""
    target = resolve_kb(packs_root, cap, rel_path)
    base = _kb_history_root(target) / "kb-backups"
    if not base.is_dir():
        return []
    flat = _flatten(target.rel)
    # <ts>[.n]__<flat>.bak
    pat = re.compile(r"(\d{8}T\d{6}Z)(?:\.\d+)?__" + re.escape(flat) + r"\.bak$")
    out = []
    for f in base.iterdir():
        m = pat.fullmatch(f.name)
        if m and f.is_file():
            st = f.stat()
            out.append({"version": f.name, "ts": m.group(1), "size": st.st_size,
                        "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(st.st_mtime))})
    out.sort(key=lambda v: v["ts"], reverse=True)
    return out


def kb_version_path(packs_root: str | Path, cap: str, rel_path: str, version: str) -> Path:
    """取版本文件的绝对路径（非法版本名/越界 → KbError，不存在由调用方判 404）。"""
    target = resolve_kb(packs_root, cap, rel_path)
    if not re.fullmatch(r"\d{8}T\d{6}Z(?:\.\d+)?__[\w .()\-]+\.bak", version):
        raise KbError(f"非法版本名: {version}")
    base = (target.source.root / ".history" / "kb-backups").resolve()
    vp = (base / version).resolve()
    if base not in vp.parents:
        raise KbError("版本路径越界")
    return vp


def rollback_kb_file(packs_root: str | Path, cap: str, rel_path: str, version: str) -> dict:
    """回滚：先把当前版再备份一次（可往返），再用历史版覆盖（文件被删后回滚=恢复）。"""
    with pack_write_lock():
        vp = kb_version_path(packs_root, cap, rel_path, version)
        if not vp.is_file():
            raise KbError(f"历史版本不存在: {version}")
        target = resolve_kb(packs_root, cap, rel_path)
        backup = None
        if target.path.is_file():
            dest = _kb_backup_path(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target.path, dest)
            backup = dest.name
        target.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vp, target.path)
        return {"path": target.module, "rolled_back": version, "current_backed_up": backup}


# ---------- 技能文件定位（提案 skill 应用复用；零 fastapi 依赖） ----------

def skill_file_path(packs_root: str | Path, skill_kind: str, owner: str,
                    skill_name: str) -> Path:
    """技能 SKILL.md 绝对路径。skill_kind: capability|track。不做存在性承诺。"""
    if skill_kind == "capability":
        return capability_dir(packs_root, owner) / "skills" / skill_name / "SKILL.md"
    if skill_kind == "track":
        return track_dir(packs_root, owner) / "skills" / skill_name / "SKILL.md"
    raise KbError(f"非法技能归属: {skill_kind}（仅 capability|track）")


def json_dump_atomic(path: Path, data: dict) -> None:
    """原子 JSON 落盘（同卷 tmp+replace），调用方负责 pack_write_lock 临界区。"""
    _json_dump(path, data)


def _json_dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
