"""对 check_intel_open_chain.py 做变异测试：撤掉每一段修复，护栏必须变红。

不做这一步的话，「护栏绿」可能只是因为它什么都没查——下次重构把链路
改断，回归依然全绿，等于没有守卫。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"E:\ILOVCTRY\cyberstrike-pro")
PY = r"E:\Miniconda3\python.exe"
GUARD = ROOT / "scripts" / "check_intel_open_chain.py"

FILES = [
    "desktop/main.js",
    "desktop/preload.js",
    "webui/src/desktop.d.ts",
    "webui/src/views/IntelView.tsx",
]

# (说明, 相对路径, 原文, 替换)
MUTATIONS = [
    (
        "去掉 main.js 成功路径日志（只留 catch）",
        "desktop/main.js",
        '      logLine(`shell:open-external 已交系统浏览器 url=${url}`)\n',
        "",
    ),
    (
        "main.js 放弃 http(s) 校验",
        "desktop/main.js",
        'if (typeof url !== "string" || !/^https?:\\/\\//i.test(url)) {',
        'if (typeof url !== "string") {',
    ),
    (
        "preload.js 改名通道（与 main.js 失联）",
        "desktop/preload.js",
        "ipcRenderer.invoke(\"shell:open-external\", url)",
        "ipcRenderer.invoke(\"shell:open-external-old\", url)",
    ),
    (
        "desktop.d.ts 删掉类型声明",
        "webui/src/desktop.d.ts",
        "openExternal?: (url: string) => Promise<boolean>",
        "openExternalRemoved?: (url: string) => Promise<boolean>",
    ),
    (
        "IntelView 退回 window.open 优先",
        "webui/src/views/IntelView.tsx",
        "const nativeOpen = window.desktopBrowser?.openExternal\n    const target = nativeOpen ? null : window.open(\"about:blank\", \"_blank\")",
        "const target = window.open(\"about:blank\", \"_blank\")\n    const nativeOpen = window.desktopBrowser?.openExternal",
    ),
    (
        "IntelView 允许把 google 中转域交给浏览器",
        "webui/src/views/IntelView.tsx",
        'if (!["http:", "https:"].includes(resolved.protocol) || resolved.hostname === "news.google.com") {',
        'if (!["http:", "https:"].includes(resolved.protocol)) {',
    ),
]


def run_guard(root: Path) -> tuple[int, str]:
    p = subprocess.run([PY, str(GUARD), str(root)], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> int:
    base_code, base_out = run_guard(ROOT)
    print(f"基线: exit={base_code} {base_out}")
    if base_code != 0:
        print("[FAIL] 基线都不绿，先修护栏")
        return 1

    failures = 0
    for desc, rel, old, new in MUTATIONS:
        tmp = Path(tempfile.mkdtemp(prefix="guard_mut_"))
        try:
            for f in FILES:
                src = ROOT / f
                dst = tmp / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            target = tmp / rel
            text = target.read_text(encoding="utf-8")
            if old not in text:
                print(f"[SKIP] {desc}: 锚点未命中（变异对输入等价，需改写变异）")
                failures += 1
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, out = run_guard(tmp)
            ok = code != 0
            print(f"[{'OK' if ok else 'MISS'}] {desc} -> exit={code}")
            if not ok:
                print("   护栏未变红，说明这条断链它查不出来")
                failures += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"[FAIL] {failures}/{len(MUTATIONS)} 个变异未被护栏拦住")
        return 1
    print(f"[PASS] {len(MUTATIONS)}/{len(MUTATIONS)} 个变异全部被拦住，护栏有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
