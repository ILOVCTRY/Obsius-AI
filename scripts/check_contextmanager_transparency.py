"""检查 `@contextmanager` 是否「透明」——`yield` 不得落在宽 except 的覆盖区内。

**为什么需要这条守卫**：`@contextmanager` 生成的上下文管理器，其 `yield` 处就是
`with` 体执行的位置。一旦 `yield` 被 `try: ... except Exception:` 包住，`with`
体内抛出的**任何异常都会被这个上下文管理器吞掉**——调用方以为「正常结束」，实际
是被静默改写控制流。这类 bug 在正常路径上完全无感（请求成功时永远走不到），
只有底层真的抛异常时才露头，表现为「一个假错误顶替了真错误」。

**真实事故（2026-10-22~24，13 例）**：`core/llm/dump.py::capture_request` 把
`yield` 包进 `except Exception`，于是传输层耗尽 11 次重试后抛的
`LLMError("网络连接失败（已重试 10 次）")` 被吞，`openai_compat.chat` 里
`status, data, note = request_with_retries(...)` 从未执行 → 上层收到
`UnboundLocalError`。它是 `NameError` 子类，既非 `LLMError` 也非 `OSError`，
于是穿过 `FallbackProvider` 的 `except (LLMError, OSError)`，跨供应商兜底
一并失效；错误分类落 `unknown`。

**判据（只报真问题，不报合法用法）**：
- 只查带 `contextmanager` / `asynccontextmanager` 装饰器的函数；
- 只看 `try` 的 **body/orelse**（真正被保护的区域），不看 handlers/finalbody；
- handler 必须是宽捕获（裸 `except:` / `except Exception` / `except BaseException`）；
- **handler 里带 `raise` 的视为合法**（如 `store._tx()`：rollback 后原样重抛，
  不吞异常），排除。

用法::

    python scripts/check_contextmanager_transparency.py [根目录...]

命中即退出码 1（供回归脚本判红）；全清退出码 0。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "dist_old_1792604100",
             "dist_old4", ".venv", "venv", ".mypy_cache", "scratch",
             "开源优秀项目"}
WIDE_EXC = {"Exception", "BaseException"}
CM_DECORATORS = ("contextmanager", "asynccontextmanager")
DEFAULT_ROOTS = ("core", "scripts", "tools", "mcp-servers", "desktop")


def _protected_yields(try_node: ast.Try) -> list[int]:
    """try 的 body/orelse 内的 yield 行号（即「被保护执行」的区域）。"""
    out: list[int] = []
    for stmt in list(try_node.body) + list(try_node.orelse):
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Yield, ast.YieldFrom)):
                out.append(node.lineno)
    return out


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    """该 handler 里是否有 raise（含裸 raise 重抛）——有则不算吞异常。"""
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _is_wide(handler: ast.ExceptHandler) -> bool:
    """裸 except / except Exception / except BaseException。"""
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in WIDE_EXC
    if isinstance(handler.type, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id in WIDE_EXC
                   for e in handler.type.elts)
    return False


def scan_file(path: Path) -> list[tuple[int, str, int]]:
    """返回 [(行号, 函数名, yield 行号)]：yield 被宽 except 覆盖且该 handler 不重抛。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []                       # 语法错误归 lint 管，这里不重复报
    hits: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
        if not any(d in decorators for d in CM_DECORATORS):
            continue
        for try_node in ast.walk(node):
            if not isinstance(try_node, ast.Try) or not try_node.handlers:
                continue
            # 只有「宽捕获且不重抛」的 handler 才会吞异常
            swallowing = [h for h in try_node.handlers
                          if _is_wide(h) and not _handler_reraises(h)]
            if not swallowing:
                continue
            for lineno in _protected_yields(try_node):
                hits.append((node.lineno, node.name, lineno))
    return hits


def main(argv: list[str]) -> int:
    roots = argv or list(DEFAULT_ROOTS)
    found: list[str] = []
    checked = 0
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            checked += 1
            for func_line, name, yield_line in scan_file(path):
                found.append(
                    f"{path.as_posix()}:{yield_line}  {name}() "
                    f"（定义于 L{func_line}）—— yield 处于 except Exception "
                    f"覆盖区，会吞掉调用方异常")
    for line in sorted(set(found)):
        print(line)
    print(f"--- 扫描 {checked} 个 .py，命中 {len(set(found))} 处")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
