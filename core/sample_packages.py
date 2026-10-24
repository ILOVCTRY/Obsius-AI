"""Immutable analysis package import and discovery primitives.

The package layer deliberately lives outside the binary asset table.  A package
is an application/game tree; binary assets remain the compatibility surface for
the existing reverse workbench.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


class SamplePackageError(ValueError):
    """A user supplied package cannot be safely imported."""


@dataclass(frozen=True)
class PackageLimits:
    max_source_bytes: int = 2 * 1024 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_expanded_bytes: int = 10 * 1024 * 1024 * 1024
    max_files: int = 100_000
    max_depth: int = 32
    #: 关联外部目录（原地索引，不复制）专用配额：这类包本来就是「很大所以不想复制」，
    #: 沿用上传配额会让几十 GB 的成品游戏目录直接 422。只放宽**扫描规模**，单文件
    #: 上限不变（单文件过大仍拒绝——那是分析侧的耗时风险，不是空间问题）。
    max_linked_files: int = 500_000
    max_linked_bytes: int = 512 * 1024 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _safe_relative(raw: str, *, max_depth: int = 32) -> str:
    value = str(raw or "").replace("\\", "/")
    if not value or "\x00" in value:
        raise SamplePackageError("文件路径为空或包含 NUL")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise SamplePackageError(f"拒绝绝对路径: {raw}")
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise SamplePackageError(f"拒绝路径穿越: {raw}")
    if any(ord(ch) < 32 for part in parts for ch in part):
        raise SamplePackageError(f"路径包含控制字符: {raw}")
    if len(parts) > max_depth:
        raise SamplePackageError(f"目录深度超过 {max_depth}: {raw}")
    return "/".join(parts)


def _is_special_mode(mode: int) -> bool:
    kind = stat.S_IFMT(mode)
    return kind not in (0, stat.S_IFREG, stat.S_IFDIR)


def classify_file(path: Path, rel_path: str) -> dict[str, Any]:
    """Return stable, dependency-free format metadata for one file."""
    suffix = Path(rel_path).suffix.lower()
    try:
        with path.open("rb") as stream:
            head = stream.read(4096)
    except OSError:
        head = b""
    fmt = "file"
    platform = "generic"
    if head.startswith(b"MZ"):
        fmt, platform = "pe", "windows"
    elif head.startswith(b"\x7fELF"):
        fmt, platform = "elf", "linux/android"
    elif head[:4] in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                      b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"):
        fmt, platform = "mach-o", "macos/ios"
    elif head.startswith(b"dex\n"):
        fmt, platform = "dex", "android"
    elif Path(rel_path).name.lower() == "androidmanifest.xml":
        fmt, platform = "android-manifest", "android"
    elif suffix in {".apk", ".aab"} and zipfile.is_zipfile(path):
        fmt, platform = suffix[1:], "android"
    elif suffix in {".zip", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}:
        fmt = "archive"
    elif suffix in {".pak", ".uasset", ".bundle", ".assets", ".resS".lower()}:
        fmt = "game-resource"
    elif suffix in {".json", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".txt", ".lua", ".js"}:
        fmt = "text"
    candidate = fmt in {"pe", "elf", "mach-o", "dex", "apk", "aab", "android-manifest", "game-resource", "archive", "text"}
    return {"format": fmt, "platform": platform, "candidate": candidate}


def _archive_kind(path: Path, filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith((".apk", ".aab", ".zip")) and zipfile.is_zipfile(path):
        return "zip"
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        try:
            if tarfile.is_tarfile(path):
                return "tar"
        except OSError:
            pass
    if lower.endswith(".7z"):
        return "7z"
    return None


def _copy_regular(src: Path, dst: Path, *, limits: PackageLimits, counters: dict[str, int]) -> None:
    if not src.is_file():
        raise SamplePackageError(f"不是普通文件: {src}")
    size = src.stat().st_size
    if size > limits.max_file_bytes:
        raise SamplePackageError(f"文件超过大小上限: {src.name}")
    if counters["files"] >= limits.max_files:
        raise SamplePackageError(f"文件数量超过 {limits.max_files}")
    if counters["bytes"] + size > limits.max_expanded_bytes:
        raise SamplePackageError("展开后的总大小超过上限")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    counters["files"] += 1
    counters["bytes"] += size


def _extract_zip(src: Path, tree: Path, *, limits: PackageLimits, counters: dict[str, int]) -> None:
    with zipfile.ZipFile(src) as archive:
        for info in archive.infolist():
            rel = _safe_relative(info.filename, max_depth=limits.max_depth)
            mode = (info.external_attr >> 16) & 0xFFFF
            if _is_special_mode(mode) or stat.S_ISLNK(mode):
                raise SamplePackageError(f"压缩包包含特殊文件: {rel}")
            if info.is_dir():
                continue
            if info.file_size > limits.max_file_bytes:
                raise SamplePackageError(f"压缩包内文件超过大小上限: {rel}")
            if counters["files"] >= limits.max_files:
                raise SamplePackageError(f"文件数量超过 {limits.max_files}")
            if counters["bytes"] + info.file_size > limits.max_expanded_bytes:
                raise SamplePackageError("展开后的总大小超过上限")
            dst = tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as inp, dst.open("wb") as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
            counters["files"] += 1
            counters["bytes"] += info.file_size


def _extract_tar(src: Path, tree: Path, *, limits: PackageLimits, counters: dict[str, int]) -> None:
    with tarfile.open(src, "r:*") as archive:
        for info in archive.getmembers():
            rel = _safe_relative(info.name, max_depth=limits.max_depth)
            if not (info.isfile() or info.isdir()) or info.issym() or info.islnk():
                raise SamplePackageError(f"压缩包包含特殊文件: {rel}")
            if info.isdir():
                continue
            if info.size > limits.max_file_bytes:
                raise SamplePackageError(f"压缩包内文件超过大小上限: {rel}")
            if counters["files"] >= limits.max_files or counters["bytes"] + info.size > limits.max_expanded_bytes:
                raise SamplePackageError("展开内容超过配额")
            dst = tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            inp = archive.extractfile(info)
            if inp is None:
                raise SamplePackageError(f"无法读取压缩包条目: {rel}")
            with inp, dst.open("wb") as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
            counters["files"] += 1
            counters["bytes"] += info.size


def _extract_7z(src: Path, tree: Path, *, limits: PackageLimits, counters: dict[str, int]) -> None:
    try:
        import py7zr  # type: ignore
    except ImportError as exc:
        raise SamplePackageError("7z 导入需要安装 py7zr") from exc
    with py7zr.SevenZipFile(src, mode="r") as archive:
        names = archive.getnames()
        for name in names:
            raw_name = str(name)
            rel = _safe_relative(raw_name.rstrip("/\\"), max_depth=limits.max_depth)
            # Validate the exact spelling before the third-party extractor sees
            # it.  A normalized path is safe for our manifest but ambiguous to
            # an extractor that treats backslashes or dot segments specially.
            exact = raw_name.rstrip("/\\").replace("\\", "/")
            if exact != rel:
                raise SamplePackageError(f"7z 条目路径不规范: {raw_name}")
            if raw_name.endswith(("/", "\\")):
                continue
        archive.extractall(path=tree)
    # Validate the extracted tree after the library has written it.  Symlinks and
    # paths outside tree are rejected before the manifest is committed.
    for item in tree.rglob("*"):
        if item.is_symlink() or (not item.is_file() and not item.is_dir()):
            raise SamplePackageError(f"7z 包含特殊条目: {item.name}")
        if item.is_file():
            _safe_relative(item.relative_to(tree).as_posix(), max_depth=limits.max_depth)
            size = item.stat().st_size
            if size > limits.max_file_bytes or counters["files"] >= limits.max_files:
                raise SamplePackageError("7z 展开内容超过配额")
            counters["bytes"] += size
            counters["files"] += 1


def _dependency_names(path: Path, fmt: str) -> set[str]:
    if fmt not in {"pe", "elf", "mach-o"}:
        return set()
    try:
        data = path.read_bytes()[:16 * 1024 * 1024]
    except OSError:
        return set()
    names = set(re.findall(rb"[A-Za-z0-9_.-]+(?:\.dll|\.so(?:\.[0-9]+)*)", data, flags=re.I))
    return {name.decode("ascii", "ignore").lower() for name in names}


def _manifest(tree: Path, *, origin: dict[str, Any], limits: PackageLimits,
              identity: str | None = None,
              progress: dict[str, Any] | None = None) -> dict[str, Any]:
    """扫描一棵树生成清单。

    ``identity``（关联外部目录专用）：外部文件夹的内容可能与另一个包完全相同，
    此时若仍只用内容哈希做包身份，两次「选择」会撞成同一个 package_id、后者的
    tree_ref 被前者的路径顶掉。把**目录绝对路径**混进哈希即可让「同一份内容、
    两个位置」保持为两个包，内容寻址的其余语义不变。

    ``progress``：可选进度字典（就地更新 files/bytes），供后台 Job 展示。
    """
    entries: list[dict[str, Any]] = []
    seen_bytes = 0
    for path in sorted((p for p in tree.rglob("*") if p.is_file()), key=lambda p: p.as_posix().lower()):
        rel = _safe_relative(path.relative_to(tree).as_posix(), max_depth=limits.max_depth)
        digest, size = sha256_file(path)
        info = classify_file(path, rel)
        entries.append({"path": rel, "sha256": digest, "size": size, **info})
        seen_bytes += size
        if progress is not None:
            progress.update({"phase": "scan", "files": len(entries), "bytes": seen_bytes})
        if len(entries) > limits.max_files:
            raise SamplePackageError(f"文件数量超过 {limits.max_files}")
    if len(entries) > limits.max_files:
        raise SamplePackageError(f"文件数量超过 {limits.max_files}")
    canonical = [{k: row[k] for k in ("path", "sha256", "size", "format", "platform")} for row in entries]
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if identity:
        payload += "\n" + identity
    manifest_hash = hashlib.sha256(payload.encode()).hexdigest()
    by_name = {Path(row["path"]).name.lower(): row for row in entries}
    dependencies: list[dict[str, str]] = []
    for row in entries:
        for name in _dependency_names(tree / row["path"], row["format"]):
            target = by_name.get(name)
            if target:
                dependencies.append({"from": row["path"], "to": target["path"], "kind": "binary-import"})
    if not entries:
        raise SamplePackageError("分析包不能为空")
    return {
        "manifest_sha256": manifest_hash,
        "created_at": utc_now(),
        "origin": origin,
        "file_count": len(entries),
        "total_bytes": sum(row["size"] for row in entries),
        "entries": entries,
        "dependencies": dependencies,
    }


def _target_id(version_id: str, path: str) -> str:
    return "target-" + hashlib.sha256(f"{version_id}:{path}".encode()).hexdigest()[:16]


ANALYZER_REGISTRY: dict[str, tuple[str, ...]] = {
    "pe": ("binary-static", "pe-metadata"),
    "elf": ("binary-static", "elf-metadata"),
    "mach-o": ("binary-static", "macho-metadata"),
    "dex": ("dex-static",),
    "apk": ("android-package",),
    "aab": ("android-package",),
    "android-manifest": ("android-manifest",),
    "game-resource": ("resource-inspect",),
    "archive": ("archive-inspect",),
    "text": ("text-inspect",),
}


def analyzers_for_target(target: dict[str, Any]) -> list[str]:
    """Return the registered analyzer ids for a manifest entry."""
    return list(ANALYZER_REGISTRY.get(str(target.get("format") or ""), ("file-inspect",)))


def _read_u4(data: bytes, offset: int, endian: str = "little") -> int | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    return int.from_bytes(data[offset:offset + 4], endian, signed=False)


def _dex_report(path: Path) -> dict[str, Any]:
    data = path.read_bytes()[:0x70]
    if len(data) < 0x70 or not data.startswith(b"dex\n"):
        return {"status": "invalid", "message": "DEX magic/header 无效"}
    endian_tag = _read_u4(data, 0x28)
    endian = "little" if endian_tag in (0x12345678, 0x78563412, None) else "little"
    fields = {
        "file_size": (0x20, 4), "header_size": (0x24, 4),
        "string_ids_size": (0x38, 4), "type_ids_size": (0x40, 4),
        "proto_ids_size": (0x48, 4), "field_ids_size": (0x50, 4),
        "method_ids_size": (0x58, 4), "class_defs_size": (0x60, 4),
    }
    values = {name: _read_u4(data, offset, endian) for name, (offset, _) in fields.items()}
    declared_size = values.get("file_size")
    actual_size = path.stat().st_size
    return {
        "status": "ok",
        "magic": data[:8].decode("ascii", "replace"),
        "version": data[4:7].decode("ascii", "replace"),
        "endian_tag": hex(endian_tag or 0),
        "actual_size": actual_size,
        "declared_size": declared_size,
        "size_matches": declared_size in (None, actual_size),
        **values,
    }


def _manifest_text_report(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("utf-8", "ignore")
    report: dict[str, Any] = {"status": "ok", "binary_xml": not text.lstrip().startswith("<")}
    if report["binary_xml"]:
        # Binary AXML keeps UTF-16LE strings in the resource table.  This is a
        # conservative inventory, not a claim that binary XML was fully parsed.
        strings = sorted(set(s.decode("utf-16le", "ignore") for s in re.findall(rb"(?:[ -~]\x00){3,}", raw)))
        report["string_count"] = len(strings)
        report["strings"] = [s for s in strings if s][:200]
        report["parse_note"] = "二进制 AXML 已提取字符串；完整资源解析由 Android 专用插件负责"
        return report
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"status": "invalid", "binary_xml": False, "message": f"Manifest XML 无法解析: {exc}"}
    android_ns = "{http://schemas.android.com/apk/res/android}"
    report["package"] = root.attrib.get("package")
    report["version_code"] = root.attrib.get(android_ns + "versionCode")
    report["version_name"] = root.attrib.get(android_ns + "versionName")
    report["permissions"] = sorted({
        node.attrib.get(android_ns + "name") for node in root.findall("uses-permission")
        if node.attrib.get(android_ns + "name")
    })
    app = root.find("application")
    components: dict[str, list[str]] = {}
    if app is not None:
        for tag in ("activity", "activity-alias", "service", "receiver", "provider"):
            components[tag] = sorted({
                node.attrib.get(android_ns + "name") for node in app.findall(tag)
                if node.attrib.get(android_ns + "name")
            })
    report["components"] = components
    report["debuggable"] = (app.attrib.get(android_ns + "debuggable") if app is not None else None)
    return report


def _archive_report(path: Path) -> dict[str, Any]:
    entries: list[str] = []
    dex: list[str] = []
    native: dict[str, list[str]] = {}
    manifest_report: dict[str, Any] | None = None
    manifest_path: str | None = None
    total_uncompressed = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            entries.append(name)
            total_uncompressed += info.file_size
            if re.fullmatch(r"(?:.*/)?classes(?:[0-9]+)?\.dex", name):
                dex.append(name)
            match = re.match(r"lib/([^/]+)/(.+\.so)$", name)
            if match:
                native.setdefault(match.group(1), []).append(match.group(2))
            if Path(name).name.lower() == "androidmanifest.xml" and manifest_report is None:
                try:
                    raw_manifest = archive.read(info)
                    temp_manifest = tempfile.NamedTemporaryFile(delete=False)
                    temp_manifest.write(raw_manifest)
                    temp_manifest.close()
                    try:
                        manifest_report = _manifest_text_report(Path(temp_manifest.name))
                    finally:
                        Path(temp_manifest.name).unlink(missing_ok=True)
                    manifest_path = name
                except (OSError, KeyError):
                    manifest_report = {"status": "invalid", "message": "无法读取 AndroidManifest.xml"}
    report: dict[str, Any] = {"status": "ok", "entry_count": len(entries),
                              "total_uncompressed": total_uncompressed,
                              "dex_files": sorted(dex),
                              "native_abis": {abi: sorted(names) for abi, names in sorted(native.items())}}
    if manifest_report is not None:
        report["manifest_path"] = manifest_path
        report["manifest"] = manifest_report
    return report


def analyze_registered_target(path: Path, target: dict[str, Any]) -> dict[str, Any] | None:
    """Run a safe, format-local analyzer; return None when no analyzer is ready."""
    fmt = str(target.get("format") or "")
    if fmt == "dex":
        return {"analyzer": "dex-static", "dex": _dex_report(path)}
    if fmt == "android-manifest":
        return {"analyzer": "android-manifest", "manifest": _manifest_text_report(path)}
    if fmt in {"apk", "aab"}:
        try:
            return {"analyzer": "android-package", "package": _archive_report(path)}
        except (OSError, zipfile.BadZipFile) as exc:
            return {"analyzer": "android-package", "package": {"status": "invalid", "message": str(exc)}}
    return None


def analyze_android_source(path: Path, filename: str) -> dict[str, Any] | None:
    """Summarize an uploaded APK/AAB before it is expanded into a package tree."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".apk", ".aab"}:
        return None
    try:
        return {"analyzer": "android-package", "format": suffix[1:],
                "package": _archive_report(path)}
    except (OSError, zipfile.BadZipFile) as exc:
        return {"analyzer": "android-package", "format": suffix[1:],
                "package": {"status": "invalid", "message": str(exc)}}


class SamplePackageStore:
    """Content-addressed immutable package store plus project metadata."""

    def __init__(self, workspace_root: str | Path, legacy_root: str | Path | None = None,
                 limits: PackageLimits | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        # 上传物一律落项目工作区内（内容树 `<project>/trees/`、原始物 `<project>/raw/`）。
        # 旧全局内容寻址存储（`data/sample-store/`）仅保留**只读**兼容——旧 manifest 的
        # 绝对 tree_ref 仍可解析，但绝不再写入、不主动 mkdir（避免在项目外重建空目录）。
        self.legacy_root = Path(
            legacy_root or self.workspace_root.parent / "data" / "sample-store").resolve()
        self.limits = limits or PackageLimits()

    def _project_root(self, project_root: str | Path) -> Path:
        root = Path(project_root).resolve()
        if not root.is_relative_to(self.workspace_root):
            raise SamplePackageError("项目目录不在工作区内")
        return root

    def _trees_root(self, project: Path) -> Path:
        return project / "trees"

    def _raw_root(self, project: Path) -> Path:
        return project / "raw"

    def _tree_allowed(self, tree: Path, project: Path) -> bool:
        """内容树位置白名单：项目内 `<project>/trees/`（新写入）或旧全局只读根（存量兼容）。"""
        return (tree.is_relative_to(self._trees_root(project))
                or tree.is_relative_to(self.legacy_root))

    def _linked_tree(self, package: dict[str, Any], project: Path) -> Path | None:
        """关联目录包的 tree_ref 校验（2026-10-24 大包原地索引）。

        关联包的内容树在**项目外**（用户自选文件夹，可能就是项目工作区本身之外
        任意位置），故不能套 `_tree_allowed` 的项目内白名单。判据改为「与登记时
        记录的目录完全一致」——路径来源是服务端自己写进 manifest 的绝对值，不
        接受请求方传入，等价于白名单。目录被删除/换盘则返回 None（调用方按
        「内容树不存在」处理）。
        """
        if not package.get("linked"):
            return None
        recorded = str((package.get("origin") or {}).get("directory") or "")
        if not recorded:
            return None
        root = Path(recorded)
        if not root.is_absolute():
            return None
        try:
            resolved = root.resolve()
        except OSError:
            return None
        try:
            if Path(str(package.get("tree_ref") or "")).resolve() != resolved:
                return None
        except OSError:
            return None
        # 关联根绝不能是项目内的 trees/raw（那会把不可变内容树当可写工作区），
        # 也不能是盘符根/工作区根本身（一次误选就把整个盘索引成包）。
        if resolved.is_relative_to(self._trees_root(project)) \
                or resolved.is_relative_to(self._raw_root(project)):
            return None
        return resolved if resolved.is_dir() else None

    def _package_tree(self, package: dict[str, Any], project: Path) -> Path | None:
        """包内容树绝对路径：关联包走 `_linked_tree`，其余走项目内白名单。"""
        tree = Path(str(package.get("tree_ref") or "")).resolve()
        if package.get("linked"):
            return self._linked_tree(package, project)
        return tree if tree.is_dir() and self._tree_allowed(tree, project) else None

    def _metadata_root(self, project_root: Path) -> Path:
        path = project_root / "sample_packages"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _current_path(self, project_root: Path) -> Path:
        return self._metadata_root(project_root) / "current.json"

    def _read_current(self, project_root: Path) -> dict[str, Any] | None:
        path = self._current_path(project_root)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _write_current(self, project_root: Path, package_id: str, version_id: str,
                       *, undo: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"package_id": package_id, "version_id": version_id,
                                   "updated_at": utc_now()}
        if undo:
            payload["undo"] = undo
        path = self._current_path(project_root)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def current_package(self, project_root: str | Path) -> dict[str, Any] | None:
        project = self._project_root(project_root)
        pointer = self._read_current(project)
        if pointer:
            return self.get_package(project, str(pointer.get("package_id") or ""),
                                    str(pointer.get("version_id") or ""))
        rows = self.list_packages(project)
        return rows[0] if rows else None

    # ---------- 执行工作区（2026-10-24「选择分析包 = 工作区」） ----------

    def exec_workspace(self, project_root: str | Path) -> Path | None:
        """当前分析包要求作为**执行工作区**的目录；无关联包 → None。

        语义：用户「选择分析包」挑的是一个大目录（成品游戏/固件树），它既是分析
        对象也是这一轮干活的 cwd——agent 的 run_cmd 相对路径、scratch 都落在它
        下面，实现与「脚本就在样本旁边、直接 ./ 跑」一致的用法。

        只对**关联目录包**（origin.source_type == "linked-directory" 且目录仍在）
        生效；上传包的内容树在 `<project>/trees/` 且不可变，绝不拿它当可写工作区。
        目录被拔盘/删除 → None（自动退回项目工作区，不阻断整个会话）。

        **只换执行根，不换产物根**（重要，2026-10-24）：平台的 artifacts/、黑板库、
        decompiler 缓存、sample_packages 元数据全部固定在 `proj.path`。把产物写进
        用户的分析目录会污染样本树（且样本目录常是只读挂载/成品游戏目录），而
        `bb_add_artifact` 的落盘路径与 DB 记录路径必须同源。
        """
        project = self._project_root(project_root)
        package = self.current_package(project)
        if not package or not package.get("linked"):
            return None
        if str((package.get("origin") or {}).get("source_type") or "") != "linked-directory":
            return None
        return self._linked_tree(package, project)

    def _commit_tree(self, project: Path, tree: Path, manifest: dict[str, Any]) -> Path:
        digest = manifest["manifest_sha256"]
        dest = self._trees_root(project) / digest
        if dest.is_dir():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        shutil.copytree(tree, temp)
        try:
            os.replace(temp, dest)
        except FileExistsError:
            shutil.rmtree(temp, ignore_errors=True)
        return dest

    def _commit_raw(self, project: Path, source: Path, raw_sha: str, filename: str) -> Path:
        suffix = Path(filename).suffix.lower()[:16]
        dest = self._raw_root(project) / f"{raw_sha}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            temp = dest.parent / f".{raw_sha}.{uuid.uuid4().hex}.tmp"
            shutil.copyfile(source, temp)
            try:
                os.replace(temp, dest)
            except FileExistsError:
                temp.unlink(missing_ok=True)
        return dest

    def import_tree(self, project_root: str | Path, tree: Path, *, origin: dict[str, Any],
                    linked: bool = False,
                    progress: dict[str, Any] | None = None) -> dict[str, Any]:
        """登记一棵内容树为分析包版本。

        ``linked=True``（2026-10-24「选择分析包」）：`tree` 就是用户自选的外部
        文件夹，**原地索引、不复制**——tree_ref 直接指向该目录，manifest 记
        `linked:true` + `origin.directory` 绝对路径。包身份哈希混入目录路径，避免
        同内容两处互相顶掉（见 `_manifest` 的 `identity`）。

        关联包与上传包的分野只有三处：①不落 `<project>/trees/`；②tree_ref 白名单
        走 `_linked_tree`；③禁止文件树编辑（`_edit_tree` 拒绝——编辑要知道临时
        全量 copytree，几十 GB 目录直接爆盘）。
        """
        project = self._project_root(project_root)
        manifest = _manifest(tree, origin=origin, limits=self.limits,
                             identity=str(origin.get("directory") or "") if linked else None,
                             progress=progress)
        if linked:
            tree_ref = Path(str(origin.get("directory"))).resolve()
        else:
            tree_ref = self._commit_tree(project, tree, manifest)
        manifest_hash = manifest["manifest_sha256"]
        package_id = "pkg-" + manifest_hash[:16]
        version_id = "ver-" + manifest_hash[:16]
        package_root = self._metadata_root(project) / package_id / "versions" / version_id
        package_root.mkdir(parents=True, exist_ok=True)
        enriched = {
            **manifest,
            "package_id": package_id,
            "version_id": version_id,
            "tree_ref": str(tree_ref),
            "linked": bool(linked),
            "targets": [
                {"target_id": _target_id(version_id, row["path"]),
                 "analyzers": analyzers_for_target(row), **row}
                for row in manifest["entries"] if row["candidate"]
            ],
        }
        (package_root / "manifest.json").write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
        imports_path = package_root.parent.parent / "imports.jsonl"
        with imports_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"imported_at": utc_now(), "version_id": version_id,
                                     "origin": origin}, ensure_ascii=False) + "\n")
        previous = self._read_current(project)
        undo = {"package_id": previous.get("package_id"), "version_id": previous.get("version_id")} if previous else None
        self._write_current(project, package_id, version_id, undo=undo)
        return enriched

    def validate_link_directory(self, project_root: str | Path,
                                directory: str | Path) -> Path:
        """关联目录的**同步预检**：只做便宜判定，不扫内容。

        必须在提交后台 Job 之前调（2026-10-24 实测踩到）：给相对路径/不存在的
        目录时，若把校验全放进 Job 里，HTTP 会先回 202、前端随后才从一个失败的
        Job 里读到错误——用户看到的是「已开始扫描」，实际什么都没发生。预检
        把这些能立刻判死的错误变成同步 4xx。

        `link_directory` 内部仍会再走一遍（Job 与直调共用同一套判据）。
        """
        project = self._project_root(project_root)
        raw = Path(directory)
        if not raw.is_absolute():
            raise SamplePackageError("分析包目录必须是绝对路径")
        try:
            root = raw.resolve(strict=True)
        except OSError as exc:
            raise SamplePackageError(f"目录不存在或不可访问: {directory}") from exc
        if not root.is_dir():
            raise SamplePackageError(f"不是目录: {directory}")
        if root.parent == root:
            raise SamplePackageError("不能把磁盘根目录作为分析包")
        for owned in (self._trees_root(project), self._raw_root(project),
                      self._metadata_root(project)):
            if root == owned or root.is_relative_to(owned):
                raise SamplePackageError(f"不能把平台内部目录作为分析包: {root}")
        if root == Path(project):
            raise SamplePackageError("不能把项目根目录本身作为分析包")
        return root

    def link_directory(self, project_root: str | Path, directory: str | Path, *,
                       progress: dict[str, Any] | None = None) -> dict[str, Any]:
        """把用户选中的**外部文件夹**原地登记为分析包（2026-10-24）。

        动机：分析包动辄几十 GB（成品游戏/整机固件树），走上传路径要么撑爆浏览器
        要么在项目内复制一份等价体积。此入口只做「扫描建清单」，文件一个字节都不
        复制——tree_ref 直接就是该目录，因此它同时也天然是该项目的执行工作区
        （`exec_workspace`，见 API 层解析）。

        三条硬约束（都是「不复制」带来的，必须显式挡）由 `validate_link_directory`
        落实：目录必须存在且是真目录；不能是盘符根；不能落在平台内部目录里。
        """
        root = self.validate_link_directory(project_root, directory)
        return self.import_tree(
            self._project_root(project_root), root,
            origin={"source_type": "linked-directory", "directory": str(root),
                    "filename": root.name},
            linked=True, progress=progress)

    def import_source(self, project_root: str | Path, source: Path, *, filename: str | None = None,
                      source_type: str = "auto") -> dict[str, Any]:
        limits = self.limits
        if not source.is_file():
            raise SamplePackageError("导入源必须是普通文件")
        if source.stat().st_size > limits.max_source_bytes:
            raise SamplePackageError("原始上传物超过大小上限")
        name = filename or source.name
        with tempfile.TemporaryDirectory(prefix="sample-package-") as temp_name:
            tree = Path(temp_name) / "tree"
            tree.mkdir()
            kind = _archive_kind(source, name) if source_type in {"auto", ""} else source_type
            if kind == "zip":
                _extract_zip(source, tree, limits=limits, counters={"files": 0, "bytes": 0})
            elif kind == "tar":
                _extract_tar(source, tree, limits=limits, counters={"files": 0, "bytes": 0})
            elif kind == "7z":
                _extract_7z(source, tree, limits=limits, counters={"files": 0, "bytes": 0})
            else:
                safe_name = _safe_relative(name, max_depth=limits.max_depth)
                _copy_regular(source, tree / safe_name, limits=limits, counters={"files": 0, "bytes": 0})
            raw_sha, raw_size = sha256_file(source)
            source_analysis = analyze_android_source(source, name)
            source_format = Path(name).suffix.lower().lstrip(".") if Path(name).suffix.lower() in {".apk", ".aab"} else (kind or "file")
            result = self.import_tree(project_root, tree, origin={
                "source_type": source_format, "filename": name,
                "raw_sha256": raw_sha, "raw_size": raw_size,
            })
            result["raw_ref"] = str(self._commit_raw(
                self._project_root(project_root), source, raw_sha, name))
            if source_analysis is not None:
                result["source_analysis"] = source_analysis
            manifest_path = (self._metadata_root(self._project_root(project_root))
                             / result["package_id"] / "versions" / result["version_id"] / "manifest.json")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["raw_ref"] = result["raw_ref"]
            if source_analysis is not None:
                payload["source_analysis"] = source_analysis
            manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

    def import_folder_files(self, project_root: str | Path, files: Iterable[tuple[str, Path]], *, origin: dict[str, Any]) -> dict[str, Any]:
        limits = self.limits
        with tempfile.TemporaryDirectory(prefix="sample-package-folder-") as temp_name:
            tree = Path(temp_name) / "tree"
            counters = {"files": 0, "bytes": 0}
            for raw_name, source in files:
                rel = _safe_relative(raw_name, max_depth=limits.max_depth)
                _copy_regular(source, tree / rel, limits=limits, counters=counters)
            return self.import_tree(project_root, tree, origin={**origin, "source_type": "folder"})

    def list_packages(self, project_root: str | Path) -> list[dict[str, Any]]:
        root = self._metadata_root(self._project_root(project_root))
        current = self._read_current(root.parent)
        if current:
            row = self.get_package(root.parent, str(current.get("package_id") or ""),
                                   str(current.get("version_id") or ""))
            return [row] if row else []
        out = []
        for manifest_path in root.glob("*/versions/*/manifest.json"):
            try:
                row = json.loads(manifest_path.read_text(encoding="utf-8"))
                imports_path = manifest_path.parents[2] / "imports.jsonl"
                row["import_count"] = sum(1 for _ in imports_path.open(encoding="utf-8")) if imports_path.is_file() else 1
                out.append(row)
            except (OSError, ValueError):
                continue
        out.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        return out

    def _edit_tree(self, project_root: str | Path, *, operation: str,
                   path: str | None = None, target_path: str | None = None,
                   files: Iterable[tuple[str, Path]] = ()) -> dict[str, Any]:
        project = self._project_root(project_root)
        package = self.current_package(project)
        if package is None:
            raise SamplePackageError("分析包不存在")
        if package.get("linked"):
            # 关联目录包是**原地索引**（tree_ref = 用户自己的文件夹），任何编辑都要
            # 先整树 copytree 到临时目录再整树扫一遍——几十 GB 目录会直接爆盘/超时，
            # 且真的写下去等于改动用户的原始数据。明确拒绝并给可行出口。
            raise SamplePackageError(
                "关联目录分析包不支持文件树编辑（原地索引，不复制内容）；"
                "请直接在其磁盘目录中增删文件后重新「选择分析包」")
        tree = Path(str(package.get("tree_ref") or "")).resolve()
        if not tree.is_dir() or not self._tree_allowed(tree, project):
            raise SamplePackageError("分析包内容树不存在或不安全")
        with tempfile.TemporaryDirectory(prefix="sample-package-edit-") as temp_name:
            working = Path(temp_name) / "tree"
            shutil.copytree(tree, working)
            if operation == "append":
                counters = {"files": 0, "bytes": 0}
                for raw_name, source in files:
                    rel = _safe_relative(raw_name, max_depth=self.limits.max_depth)
                    destination = working / rel
                    if destination.exists():
                        stem, suffix = destination.stem, destination.suffix
                        index = 1
                        while destination.exists():
                            destination = destination.with_name(f"{stem} ({index}){suffix}")
                            index += 1
                        rel = destination.relative_to(working).as_posix()
                    _copy_regular(source, destination, limits=self.limits, counters=counters)
            elif operation == "move":
                src = working / _safe_relative(str(path or ""), max_depth=self.limits.max_depth)
                dest_dir = working / _safe_relative(str(target_path or ""), max_depth=self.limits.max_depth)
                if not src.exists() or not dest_dir.is_dir():
                    raise SamplePackageError("移动源或目标目录不存在")
                destination = dest_dir / src.name
                if destination.exists():
                    stem, suffix = destination.stem, destination.suffix
                    index = 1
                    while destination.exists():
                        destination = dest_dir / f"{stem} ({index}){suffix}"
                        index += 1
                if destination == src or destination.is_relative_to(src):
                    raise SamplePackageError("不能移动到自身或子目录")
                shutil.move(str(src), str(destination))
            elif operation == "delete":
                target = working / _safe_relative(str(path or ""), max_depth=self.limits.max_depth)
                if not target.exists():
                    raise SamplePackageError("删除目标不存在")
                if target == working:
                    raise SamplePackageError("不能删除分析包根目录")
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            else:
                raise SamplePackageError("未知分析包变更")
            return self.import_tree(project, working, origin={"source_type": "edit", "operation": operation})

    def append_files(self, project_root: str | Path, files: Iterable[tuple[str, Path]]) -> dict[str, Any]:
        return self._edit_tree(project_root, operation="append", files=files)

    def move_entry(self, project_root: str | Path, path: str, target_path: str) -> dict[str, Any]:
        return self._edit_tree(project_root, operation="move", path=path, target_path=target_path)

    def delete_entry(self, project_root: str | Path, path: str) -> dict[str, Any]:
        return self._edit_tree(project_root, operation="delete", path=path)

    def undo_last(self, project_root: str | Path) -> dict[str, Any]:
        project = self._project_root(project_root)
        pointer = self._read_current(project)
        undo = (pointer or {}).get("undo")
        if not isinstance(undo, dict) or not undo.get("package_id") or not undo.get("version_id"):
            raise SamplePackageError("没有可撤销的文件树变更")
        previous = self.get_package(project, str(undo["package_id"]), str(undo["version_id"]))
        if previous is None:
            raise SamplePackageError("撤销目标版本不存在")
        # A single undo is deliberately consumed and is not itself undoable.
        self._write_current(project, previous["package_id"], previous["version_id"])
        return previous

    def delete_package(self, project_root: str | Path, package_id: str | None = None) -> dict[str, Any]:
        """Remove the project's analysis package metadata and current pointer.

        Content addressed trees live under `<project>/trees/` and are shared by
        design (another package in the same project may reuse the same tree), so
        they are left in place.  Project-local manifests, reports and import
        history are removed by deleting the package metadata directory.

        **关联目录包（linked）同样只删元数据**：tree_ref 是用户自己的文件夹，
        删包=解除登记，绝不动用户的原始目录（这一条是硬红线——「删除分析包」
        按钮对几十 GB 的外部目录不能有任何写操作）。
        """
        project = self._project_root(project_root)
        pointer = self._read_current(project)
        selected = str(package_id or (pointer or {}).get("package_id") or "")
        if not selected:
            current = self.current_package(project)
            selected = str((current or {}).get("package_id") or "")
        if not re.fullmatch(r"pkg-[0-9a-f]{16}", selected):
            raise SamplePackageError("分析包不存在")
        package_root = self._metadata_root(project) / selected
        if not package_root.is_dir():
            raise SamplePackageError("分析包不存在")
        shutil.rmtree(package_root)
        current_path = self._current_path(project)
        if current_path.is_file():
            current_path.unlink()
        return {"deleted": selected}

    def get_package(self, project_root: str | Path, package_id: str, version_id: str | None = None) -> dict[str, Any] | None:
        root = self._metadata_root(self._project_root(project_root)) / package_id / "versions"
        paths = [root / version_id / "manifest.json"] if version_id else sorted(root.glob("*/manifest.json"), reverse=True)
        for path in paths:
            if path.is_file():
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                    imports_path = path.parents[2] / "imports.jsonl"
                    if imports_path.is_file():
                        history = []
                        lines = imports_path.read_text(encoding="utf-8").splitlines()
                        for line in lines:
                            try:
                                item = json.loads(line)
                            except ValueError:
                                continue
                            if item.get("version_id") == row.get("version_id"):
                                history.append(item)
                        row["import_history"] = history
                        row["import_count"] = len(lines)
                    else:
                        row["import_count"] = 1
                    return row
                except (OSError, ValueError):
                    return None
        return None

    def select_targets(self, project_root: str | Path, package_id: str, version_id: str,
                       target_ids: list[str]) -> dict[str, Any]:
        package = self.get_package(project_root, package_id, version_id)
        if package is None:
            raise SamplePackageError("分析包版本不存在")
        known = {row["target_id"] for row in package.get("targets", [])}
        selected = []
        for target_id in target_ids:
            if target_id not in known:
                raise SamplePackageError(f"未知分析目标: {target_id}")
            selected.append(target_id)
        path = (self._metadata_root(self._project_root(project_root)) / package_id
                / "selections.json")
        current = {"package_id": package_id, "version_id": version_id,
                   "target_ids": sorted(set(selected)), "updated_at": utc_now()}
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return current

    def resolve_target(self, project_root: str | Path, package_id: str,
                       version_id: str, target_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
        """Resolve a target to its content-addressed, read-only file path."""
        project = self._project_root(project_root)
        package = self.get_package(project, package_id, version_id)
        if package is None:
            raise SamplePackageError("分析包版本不存在")
        target = next((row for row in package.get("targets", [])
                       if row.get("target_id") == target_id), None)
        if target is None:
            raise SamplePackageError("分析目标不存在")
        if not target.get("analyzers"):
            target = {**target, "analyzers": analyzers_for_target(target)}
        tree = self._package_tree(package, project)
        if tree is None:
            raise SamplePackageError("分析包内容树不存在或不安全")
        rel = _safe_relative(str(target.get("path") or ""), max_depth=self.limits.max_depth)
        path = (tree / rel).resolve()
        if not path.is_file() or not path.is_relative_to(tree):
            raise SamplePackageError("分析目标文件不存在或越界")
        return package, target, path

    def resolve_entry_path(self, project_root: str | Path, path: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
        """Resolve one current package entry for safe, read-only preview."""
        project = self._project_root(project_root)
        package = self.current_package(project)
        if package is None:
            raise SamplePackageError("分析包不存在")
        rel = _safe_relative(path, max_depth=self.limits.max_depth)
        entry = next((row for row in package.get("entries", []) if row.get("path") == rel), None)
        if entry is None:
            raise SamplePackageError("文件不存在")
        tree = self._package_tree(package, project)
        if tree is None:
            raise SamplePackageError("分析包内容树不存在或不安全")
        resolved = (tree / rel).resolve()
        if not resolved.is_file() or not resolved.is_relative_to(tree):
            raise SamplePackageError("文件不存在或越界")
        return package, entry, resolved

    def preview_entry(self, project_root: str | Path, path: str,
                      *, max_bytes: int = 256 * 1024) -> dict[str, Any]:
        """Return bounded, non-executing data for an editor-style file preview."""
        if max_bytes < 4096 or max_bytes > 2 * 1024 * 1024:
            raise SamplePackageError("预览大小必须在 4KB 到 2MB 之间")
        package, entry, resolved = self.resolve_entry_path(project_root, path)
        size = resolved.stat().st_size
        with resolved.open("rb") as stream:
            data = stream.read(max_bytes)
        fmt = str(entry.get("format") or "file")
        suffix = resolved.suffix.lower()
        image_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                       ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
        kind = "binary"
        if suffix in image_types or data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8")):
            kind = "image"
        elif fmt in {"text", "android-manifest"} or suffix in {".json", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".txt", ".md", ".lua", ".js", ".py", ".toml"}:
            kind = "text"
        elif fmt in {"archive", "apk", "aab"}:
            kind = "structure"
        elif fmt == "dex":
            kind = "structure"
        result: dict[str, Any] = {
            "path": str(entry.get("path") or path),
            "format": fmt,
            "size": size,
            "sha256": entry.get("sha256"),
            "kind": kind,
            "truncated": size > len(data),
            "preview_bytes": len(data),
            "content_url": None,
        }
        if kind == "image":
            result["media_type"] = next((mime for ext, mime in image_types.items() if suffix == ext), "application/octet-stream")
        if kind == "text":
            result["text"] = data.decode("utf-8", "replace")
        if kind == "structure":
            if fmt in {"archive", "apk", "aab"}:
                try:
                    result["archive"] = _archive_report(resolved)
                except (OSError, zipfile.BadZipFile) as exc:
                    result["archive"] = {"status": "invalid", "message": str(exc)}
            elif fmt == "dex":
                result["dex"] = _dex_report(resolved)
        result["hex_rows"] = [
            {"offset": index, "hex": chunk.hex(" "),
             "ascii": "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)}
            for index in range(0, len(data), 16)
            for chunk in [data[index:index + 16]]
        ]
        return result

    def _target_analysis_path(self, project_root: str | Path, package_id: str,
                              version_id: str, target_id: str) -> Path:
        root = self._metadata_root(self._project_root(project_root))
        if not re.fullmatch(r"pkg-[0-9a-f]{16}", package_id) \
                or not re.fullmatch(r"ver-[0-9a-f]{16}", version_id) \
                or not re.fullmatch(r"target-[0-9a-f]{16}", target_id):
            raise SamplePackageError("分析目标标识非法")
        path = root / package_id / "versions" / version_id / "targets" / f"{target_id}.json"
        if not path.resolve().is_relative_to(root):
            raise SamplePackageError("分析结果路径越界")
        return path

    def read_target_analysis(self, project_root: str | Path, package_id: str,
                             version_id: str, target_id: str) -> dict[str, Any] | None:
        path = self._target_analysis_path(project_root, package_id, version_id, target_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def write_target_analysis(self, project_root: str | Path, package_id: str,
                              version_id: str, target_id: str, report: dict[str, Any]) -> dict[str, Any]:
        path = self._target_analysis_path(project_root, package_id, version_id, target_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        return report
