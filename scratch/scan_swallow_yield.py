"""扫描：contextmanager 里 yield 被宽泛 except 覆盖（会吞掉调用方真实异常）。"""
import ast
import os
import sys

SKIP = {".git", "node_modules", "__pycache__", "dist", "dist_old_1792604100",
        "dist_old4", ".venv", "venv", "openhands", "Knowledge"}
WIDE = {"Exception", "BaseException"}


def yield_positions(try_node):
    """返回该 try 中真正处于「被保护执行」区域（body/orelse）的 yield 行号。"""
    out = []
    for st in list(try_node.body) + list(try_node.orelse):
        for n in ast.walk(st):
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                out.append(n.lineno)
    return out


def main(roots):
    hits = []
    for root in roots:
        for dp, dns, fs in os.walk(root):
            dns[:] = [d for d in dns if d not in SKIP]
            for f in fs:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(p, encoding="utf-8", errors="replace").read())
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    decs = " ".join(ast.unparse(d) for d in node.decorator_list)
                    if "contextmanager" not in decs:
                        continue
                    for t in ast.walk(node):
                        if not isinstance(t, ast.Try) or not t.handlers:
                            continue
                        wide = any(
                            h.type is None
                            or (isinstance(h.type, ast.Name) and h.type.id in WIDE)
                            for h in t.handlers)
                        if not wide:
                            continue
                        for ln in yield_positions(t):
                            hits.append((p, node.name, ln))
    seen = set()
    for h in hits:
        if h in seen:
            continue
        seen.add(h)
        print(f"{h[0]}:{h[2]}  {h[1]}()  —— yield 处于 except Exception 覆盖区")
    print(f"--- {len(seen)} 处")


if __name__ == "__main__":
    main(sys.argv[1:] or ["."])
