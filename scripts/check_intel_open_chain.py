"""检查「情报检索结果 → 系统浏览器」外链链路的四段是否仍然完整。

**为什么需要这条守卫**：桌面壳的 `guardTopLevelNavigation` 里，`setWindowOpenHandler`
对**任何** `window.open`（包括 `about:blank`）都返回 `deny`，调用方拿到 `null`。
所以「点搜索结果打开浏览器」这件事不能靠 `window.open` 承接——必须走主进程的
`shell:open-external`。这条链路横跨四个文件：

    渲染进程 IntelView.tsx  →  preload.js  →  main.js  →  系统浏览器
                    ↑ 类型镜像 desktop.d.ts

任何一段被单独改掉（重命名、删字段、改校验），链路都会**静默断掉**：
界面上点击毫无反应，控制台也不报错。2026-10-25 排查「点击不会打开浏览器」
时，就因为渲染侧取证对象是 contextBridge 冻结对象而拿到假阴性，
绕了很久才在主进程日志里定位到真相。

**判据**（逐段静态断言，缺一即红）：
1. `main.js` 注册 `shell:open-external`，只放行 http(s)，且**成功与失败都写日志**
   —— 只记失败会让成功路径在主进程完全不可见，取证时无从下手；
2. `preload.js` 在 `desktopBrowser` 上暴露 `openExternal`，通道名与 (1) 一致；
3. `desktop.d.ts` 有对应类型声明（漏了会让 TS 侧静默回退到 `window.open`）；
4. `IntelView.tsx` 的 `openSearchResult`：先 `preventDefault`、优先用原生通道、
   解析结果不得回落到 `news.google.com`。

用法::

    python scripts/check_intel_open_chain.py [根目录]

命中即退出码 1（供回归判红）；全清退出码 0。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANNEL = "shell:open-external"


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise SystemExit(f"[拒绝] 缺少文件 {rel}")
    return path.read_text(encoding="utf-8", errors="replace")


def check_main(text: str) -> list[str]:
    bad: list[str] = []
    if f'ipcMain.handle("{CHANNEL}"' not in text:
        bad.append(f'main.js 未注册 ipcMain.handle("{CHANNEL}")')
        return bad
    # 截取该 handler 到下一个 ipcMain.handle
    start = text.index(f'ipcMain.handle("{CHANNEL}"')
    rest = text[start:]
    nxt = rest.find("ipcMain.handle(", len(CHANNEL) + 20)
    body = rest if nxt == -1 else rest[:nxt]
    if not re.search(r"https\?", body):
        bad.append("main.js 外链通道未做 http(s) 协议校验")
    if "shell.openExternal" not in body:
        bad.append("main.js 外链通道未调用 shell.openExternal")
    # 成功路径必须留痕：只看 `await shell.openExternal` **之后**的文本。
    # 若整段扫 logLine，拒绝分支（非法 url）的日志会冒充成功日志，
    # 撤掉成功日志也查不出来（变异测试实测漏网）。
    call_at = body.find("await shell.openExternal")
    after_call = "" if call_at == -1 else body[call_at:]
    catch_at = after_call.find("catch")
    success_path = after_call if catch_at == -1 else after_call[:catch_at]
    if "logLine" not in success_path:
        bad.append("main.js 成功打开后未写日志（取证时主进程完全不可见）")
    return bad


def check_preload(text: str) -> list[str]:
    bad: list[str] = []
    if "openExternal" not in text:
        bad.append("preload.js 未暴露 openExternal")
        return bad
    # 通道名必须**逐字相等**：用子串判定时，改成 `shell:open-external-old`
    # 仍能命中原串，链路其实已失联却判绿（变异测试实测漏网）。
    channels = re.findall(r'invoke\(\s*["\']([^"\']+)["\']', text)
    if CHANNEL not in channels:
        bad.append(f"preload.js 未以 {CHANNEL} 作为 invoke 通道名（实得 {channels or '空'}）")
    if not re.search(r"openExternal\s*:\s*\([^)]*\)\s*=>\s*ipcRenderer\.invoke", text):
        bad.append("preload.js 的 openExternal 未走 ipcRenderer.invoke")
    return bad


def check_types(text: str) -> list[str]:
    if not re.search(r"openExternal\s*\??\s*:", text):
        return ["desktop.d.ts 缺少 openExternal 类型声明（TS 侧会静默回退 window.open）"]
    return []


def _fn_body(text: str, marker: str) -> str:
    """取 `marker` 开始到同缩进的 `}` 收尾，粗粒度够用。"""
    at = text.find(marker)
    if at == -1:
        return ""
    tail = text[at:]
    end = tail.find("\n  }")
    return tail if end == -1 else tail[:end]


def _strip_comments(code: str) -> str:
    """去掉 `//` 行注释与 `/* */` 块注释。

    注释里会自然提到 `window.open`（解释「为什么不能靠它」），
    不剔除就会把「解释性提及」误判成「实际调用」，产出假红。
    """
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"^\s*//.*$", "", code, flags=re.M)


def check_view(text: str) -> list[str]:
    body = _fn_body(text, "const openSearchResult")
    if not body:
        return ["IntelView.tsx 找不到 openSearchResult（点击链路已丢失）"]
    code = _strip_comments(body)
    bad: list[str] = []
    if "preventDefault" not in code:
        bad.append("openSearchResult 未 preventDefault，会先被 <a target=_blank> 导航抢走")
    if "openExternal" not in code:
        bad.append("openSearchResult 未使用原生 openExternal 通道")
    if "window.open" not in code:
        bad.append("openSearchResult 缺少纯浏览器环境下的 window.open 回落")
    # 原生通道必须优先：nativeOpen 的取值要早于 window.open 调用
    i_native = code.find("nativeOpen =")
    i_open = code.find("window.open(")
    if i_native == -1 or (i_open != -1 and i_native > i_open):
        bad.append("openSearchResult 未优先使用原生通道（window.open 在前）")
    # 解析结果不得是 google 中转域
    if not re.search(r"hostname\s*===?\s*[\"']news\.google\.com[\"']", code):
        bad.append("openSearchResult 未拒绝把 news.google.com 交给浏览器（中转页打不开）")
    return bad


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    problems: list[str] = []
    problems += check_main(_read(root, "desktop/main.js"))
    problems += check_preload(_read(root, "desktop/preload.js"))
    problems += check_types(_read(root, "webui/src/desktop.d.ts"))
    problems += check_view(_read(root, "webui/src/views/IntelView.tsx"))

    if problems:
        print(f"[拒绝] 情报外链链路有 {len(problems)} 处断裂：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("[通过] 情报外链链路四段齐全：main.js / preload.js / desktop.d.ts / IntelView.tsx")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
