#!/usr/bin/env python3
"""Local automation runner for Android reverse-engineering CTF tasks.

The runner deliberately separates automation facts from model conclusions. It creates
an output workspace containing extracted APK contents, tool outputs, JSON results, and
a Markdown report. Known challenge-specific plugins can add deterministic solving.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

# Allow direct execution from tools/android without installing a package.
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import wbox_solve
import ali_crackme3_solve


@dataclass
class ToolStatus:
    name: str
    path: str | None
    available: bool


@dataclass
class RunnerResult:
    apk: str
    workspace: str
    tool_status: list[ToolStatus]
    archive_entries: list[str]
    dex_files: list[str]
    native_libs: list[str]
    java_hints: list[str]
    native_hints: list[str]
    plugins_triggered: list[str]
    plugin_results: dict
    report: str


def run(cmd: list[str], cwd: Path | None = None, input_bytes: bytes | None = None, timeout: int = 120) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"").decode("utf-8", "replace"), "timeout"


def which_many(names: Iterable[str]) -> list[ToolStatus]:
    out = []
    for name in names:
        p = shutil.which(name)
        out.append(ToolStatus(name=name, path=p, available=bool(p)))
    return out


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def extract_apk(apk: Path, out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(apk) as zf:
        zf.extractall(out_dir)
        return zf.namelist()


def strings_bytes(data: bytes, min_len: int = 4) -> list[str]:
    result = []
    cur = bytearray()
    for b in data:
        if 32 <= b <= 126:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                result.append(cur.decode("ascii", "replace"))
            cur.clear()
    if len(cur) >= min_len:
        result.append(cur.decode("ascii", "replace"))
    return result


def collect_file_strings(path: Path, limit: int = 2000) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    vals = strings_bytes(data)
    # Deduplicate preserving order.
    seen = set()
    out = []
    for s in vals:
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def hint_filter(strings: list[str]) -> list[str]:
    keywords = [
        "flag", "ctf", "System.loadLibrary", "loadLibrary", "native", "JNI",
        "Java_", "md5", "sha", "aes", "base64", "morse", "wbox", "ch(",
        ".--", "WJmk", "WOJI", "GetStringUTFChars", "strcmp", "memcmp",
    ]
    out = []
    for s in strings:
        low = s.lower()
        if any(k.lower() in low for k in keywords):
            out.append(s)
    return out[:200]


def run_optional_tools(apk: Path, workspace: Path, extract_dir: Path, native_libs: list[str]) -> None:
    logs = workspace / "tool_logs"
    logs.mkdir(parents=True, exist_ok=True)

    if shutil.which("jadx"):
        code, out, err = run(["jadx", "-d", str(workspace / "jadx"), str(apk)], timeout=300)
        write_text(logs / "jadx.log", f"exit={code}\nSTDOUT:\n{out}\nSTDERR:\n{err}")

    if shutil.which("apktool"):
        code, out, err = run(["apktool", "d", "-f", str(apk), "-o", str(workspace / "apktool")], timeout=300)
        write_text(logs / "apktool.log", f"exit={code}\nSTDOUT:\n{out}\nSTDERR:\n{err}")

    for rel in native_libs:
        so = extract_dir / rel
        safe = rel.replace("/", "_")
        if shutil.which("readelf"):
            code, out, err = run(["readelf", "-Ws", str(so)], timeout=120)
            write_text(logs / f"readelf_{safe}.txt", f"exit={code}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
        objdump = shutil.which("arm-linux-gnueabi-objdump") or shutil.which("llvm-objdump") or shutil.which("objdump")
        if objdump:
            args = [objdump, "-d", str(so)]
            if Path(objdump).name == "llvm-objdump":
                args = [objdump, "-d", "--triple=armv7-none-linux-android", str(so)]
            code, out, err = run(args, timeout=180)
            write_text(logs / f"objdump_{safe}.txt", f"exit={code}\nSTDOUT:\n{out[:200000]}\nSTDERR:\n{err}")


def maybe_run_wbox_plugin(apk: Path, native_libs: list[str], workspace: Path) -> dict | None:
    if not any(lib.endswith("libwbox.so") for lib in native_libs):
        return None
    result_json = workspace / "plugins" / "wbox_result.json"
    result = wbox_solve.solve(apk)
    result_json.parent.mkdir(parents=True, exist_ok=True)
    result_json.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    return asdict(result)


def maybe_run_ali_crackme3_plugin(apk: Path, native_libs: list[str], workspace: Path) -> dict | None:
    if ali_crackme3_solve.NATIVE_LIB not in native_libs:
        return None
    result = ali_crackme3_solve.solve(apk)
    inspection = result.get("apk_inspection", {})
    if not (inspection.get("apk_sha256_matches_known") or inspection.get("native_lib_sha256_matches_known")):
        return None
    result_json = workspace / "plugins" / "ali_crackme3_result.json"
    result_json.parent.mkdir(parents=True, exist_ok=True)
    result_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def build_report(result: RunnerResult) -> str:
    lines = []
    lines.append("# Android CTF Automation Report")
    lines.append("")
    lines.append("## APK")
    lines.append("")
    lines.append(f"- Path: `{result.apk}`")
    lines.append(f"- Workspace: `{result.workspace}`")
    lines.append("")
    lines.append("## Tool availability")
    lines.append("")
    for t in result.tool_status:
        lines.append(f"- {t.name}: {'yes' if t.available else 'no'}{(' - ' + t.path) if t.path else ''}")
    lines.append("")
    lines.append("## Archive triage")
    lines.append("")
    lines.append(f"- DEX files: `{', '.join(result.dex_files) if result.dex_files else 'none'}`")
    lines.append(f"- Native libs: `{', '.join(result.native_libs) if result.native_libs else 'none'}`")
    lines.append("")
    if result.java_hints:
        lines.append("## Java / DEX hints")
        lines.append("")
        for h in result.java_hints[:80]:
            lines.append(f"- `{h}`")
        lines.append("")
    if result.native_hints:
        lines.append("## Native hints")
        lines.append("")
        for h in result.native_hints[:80]:
            lines.append(f"- `{h}`")
        lines.append("")
    lines.append("## Plugin results")
    lines.append("")
    if not result.plugins_triggered:
        lines.append("No known challenge plugin triggered. Continue with manual Java/JNI/native tracing.")
    else:
        for name in result.plugins_triggered:
            lines.append(f"### {name}")
            pr = result.plugin_results.get(name, {})
            if name == "wbox":
                lines.append("")
                lines.append(f"- Final input: `{pr.get('final_input')}`")
                lines.append(f"- Flag: `{pr.get('flag')}`")
                lines.append(f"- Verification: `{'passed' if pr.get('verification_passed') else 'failed'}`")
                lines.append(f"- Target ciphertext: `{pr.get('target_ciphertext')}`")
                lines.append(f"- AES key: `{pr.get('aes_key')}`")
                lines.append(f"- Runtime add const: `{pr.get('runtime_add_const')}`")
                lines.append(f"- Recovered initialized buffer: `{pr.get('recovered_buf0')}`")
                lines.append("")
                lines.append("Evidence chain:")
                lines.append("")
                lines.append("```text")
                lines.append("Java Morse string -> decoy/intermediate clue")
                lines.append("System.loadLibrary('wbox') -> libwbox.so")
                lines.append("Java_k2015_a2_Ch_ch -> sub_1de0 -> sub_24c8 -> sub_14a4 -> AES-128-ECB compare")
                lines.append("AES decrypt target -> remove runtime add const -> remove index-add transform -> recover user prefix")
                lines.append("```")
            elif name == "ali_crackme3":
                inspection = pr.get("apk_inspection", {})
                lines.append("")
                lines.append(f"- Package: `{pr.get('package')}`")
                lines.append(f"- Native lib: `{pr.get('native_lib')}`")
                lines.append(f"- APK SHA256: `{inspection.get('apk_sha256')}`")
                lines.append(f"- SO SHA256: `{inspection.get('native_lib_sha256')}`")
                lines.append(f"- Flag body / app input: `{pr.get('flag_body')}`")
                lines.append(f"- Flag: `{pr.get('flag')}`")
                lines.append(f"- Verification: `{pr.get('verification')}`")
                lines.append("")
                lines.append("Evidence chain:")
                lines.append("")
                lines.append("```text")
                lines.append("com.ctf.crackme3.MainActivity.check(String)")
                lines.append("  -> System.loadLibrary('crackme')")
                lines.append("  -> lib/armeabi/libcrackme.so")
                lines.append("  -> restored native table transform")
                lines.append("  -> decrypt 256-byte table with xor key 20 8e 13 39, rotations, PRGA-like sbox xor, xor 0x29")
                lines.append("  -> first 15 restored bytes become accepted app input")
                lines.append("```")
    lines.append("")
    lines.append("## Next manual steps")
    lines.append("")
    lines.append("- Inspect `tool_logs/` for readelf/objdump output.")
    lines.append("- If no plugin solved it, use jadx/apktool output to trace UI and JNI.")
    lines.append("- Add a new plugin once the transform and constants are understood.")
    lines.append("")
    return "\n".join(lines)


def analyze(apk: Path, workspace: Path, run_tools: bool = True) -> RunnerResult:
    workspace = workspace.resolve()
    extract_dir = workspace / "extract"
    workspace.mkdir(parents=True, exist_ok=True)

    archive_entries = extract_apk(apk, extract_dir)
    dex_files = [e for e in archive_entries if e.endswith(".dex")]
    native_libs = [e for e in archive_entries if e.endswith(".so")]

    tool_status = which_many([
        "apktool", "jadx", "readelf", "objdump", "llvm-objdump",
        "arm-linux-gnueabi-objdump", "strings", "rizin", "radare2", "openssl",
    ])

    all_java_strings = []
    for rel in dex_files:
        all_java_strings.extend(collect_file_strings(extract_dir / rel))
    java_hints = hint_filter(all_java_strings)

    all_native_strings = []
    for rel in native_libs:
        all_native_strings.extend(collect_file_strings(extract_dir / rel))
    native_hints = hint_filter(all_native_strings)

    if run_tools:
        run_optional_tools(apk, workspace, extract_dir, native_libs)

    plugins_triggered: list[str] = []
    plugin_results: dict = {}
    wbox = maybe_run_wbox_plugin(apk, native_libs, workspace)
    if wbox is not None:
        plugins_triggered.append("wbox")
        plugin_results["wbox"] = wbox
    ali_crackme3 = maybe_run_ali_crackme3_plugin(apk, native_libs, workspace)
    if ali_crackme3 is not None:
        plugins_triggered.append("ali_crackme3")
        plugin_results["ali_crackme3"] = ali_crackme3

    result = RunnerResult(
        apk=str(apk.resolve()),
        workspace=str(workspace),
        tool_status=tool_status,
        archive_entries=archive_entries,
        dex_files=dex_files,
        native_libs=native_libs,
        java_hints=java_hints,
        native_hints=native_hints,
        plugins_triggered=plugins_triggered,
        plugin_results=plugin_results,
        report=str(workspace / "report.md"),
    )

    report = build_report(result)
    write_text(workspace / "report.md", report)
    write_text(workspace / "result.json", json.dumps(asdict(result), indent=2, ensure_ascii=False))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Android CTF reverse automation runner")
    parser.add_argument("apk", help="Path to APK")
    parser.add_argument("--workspace", "-w", default="android_ctf_out", help="Output workspace")
    parser.add_argument("--no-tools", action="store_true", help="Skip optional external jadx/apktool/readelf/objdump runs")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(argv)

    apk = Path(args.apk).resolve()
    if not apk.exists():
        parser.error(f"APK not found: {apk}")

    result = analyze(apk, Path(args.workspace), run_tools=not args.no_tools)

    if args.json:
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    else:
        print(f"report = {result.report}")
        if "wbox" in result.plugin_results:
            pr = result.plugin_results["wbox"]
            print(f"final_input = {pr['final_input']}")
            print(f"flag = {pr['flag']}")
            print(f"verification = {'passed' if pr['verification_passed'] else 'failed'}")
        elif "ali_crackme3" in result.plugin_results:
            pr = result.plugin_results["ali_crackme3"]
            print(f"final_input = {pr['flag_body']}")
            print(f"flag = {pr['flag']}")
            print(f"verification = {pr['verification']}")
        else:
            print("no solver plugin triggered; inspect report.md and tool_logs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
