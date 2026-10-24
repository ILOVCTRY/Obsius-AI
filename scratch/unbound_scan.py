"""UnboundLocal 分析器（严格 must-defined 数据流）。

用法: python scratch/unbound_scan.py [变量名] [根目录...]

输出：每个函数里「读取某局部变量时它可能尚未绑定」的位置。
"""
from __future__ import annotations

import ast
import os
import sys

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "dist", "dist_old_1792604100",
    "dist_old4", ".venv", "venv", ".mypy_cache", ".pytest_cache", "build",
    "site-packages", "openhands", "reg4.log", "Knowledge",
}


def _walk_names(node, *, skip_nested=False):
    """产出该子树内的 Name 节点；skip_nested 时不下钻到嵌套函数/类/lambda。"""
    stack = [node]
    while stack:
        n = stack.pop()
        if skip_nested and isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(n, ast.Name):
            yield n
        for child in ast.iter_child_nodes(n):
            stack.append(child)


class Analyzer:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.problems = []

    # ---- 表达式 ----
    def expr(self, node, defined, where):
        if node is None:
            return
        for n in _walk_names(node):
            if n.id == self.name and isinstance(n.ctx, ast.Load) and n.id not in defined:
                self.problems.append((self.path, where, n.lineno,
                                      ast.unparse(node)[:120]))

    def stmt(self, node, defined, fn, depth=0):
        """检查一条语句中的所有读取，返回执行后**必然**已定义的集合。"""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 函数定义体是另一个作用域，单独分析
            self.function(node)
            return defined | {node.name}
        if isinstance(node, ast.ClassDef):
            for b in node.body:
                self.stmt(b, set(defined), fn, depth + 1)
            return defined | {node.name}

        if isinstance(node, ast.If):
            self.expr(node.test, defined, fn)
            d1 = self.block(node.body, set(defined), fn, depth)
            if node.orelse:
                d2 = self.block(node.orelse, set(defined), fn, depth)
                return d1 & d2
            return set(defined)      # 无 else：可能不执行

        if isinstance(node, (ast.For, ast.AsyncFor)):
            self.expr(node.iter, defined, fn)
            inner = set(defined)
            for n in _walk_names(node.target):
                if isinstance(n.ctx, ast.Store):
                    inner.add(n.id)
            self.block(node.body, set(inner), fn, depth)
            if node.orelse:
                self.block(node.orelse, set(defined), fn, depth)
            return set(defined)      # 循环体可能一次都不跑

        if isinstance(node, ast.While):
            self.expr(node.test, defined, fn)
            self.block(node.body, set(defined), fn, depth)
            if node.orelse:
                self.block(node.orelse, set(defined), fn, depth)
            return set(defined)

        if isinstance(node, (ast.With, ast.AsyncWith)):
            d = set(defined)
            for item in node.items:
                self.expr(item.context_expr, d, fn)
                if item.optional_vars is not None:
                    for n in _walk_names(item.optional_vars):
                        if isinstance(n.ctx, ast.Store):
                            d.add(n.id)
            return self.block(node.body, d, fn, depth)

        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            d_try = self.block(node.body, set(defined), fn, depth)
            if node.orelse:
                d_try = self.block(node.orelse, d_try, fn, depth)
            outs = [d_try]
            for h in node.handlers:
                d_h = set(defined)
                if h.type is not None:
                    self.expr(h.type, d_h, fn)
                if h.name:
                    d_h.add(h.name)
                outs.append(self.block(h.body, d_h, fn, depth))
            merged = outs[0]
            for o in outs[1:]:
                merged = merged & o
            if node.finalbody:
                d_fin = self.block(node.finalbody, set(merged), fn, depth)
                # finally 里 return/raise 不影响；保守取并集
                merged = merged | d_fin
            return merged

        if isinstance(node, ast.Match):
            self.expr(node.subject, defined, fn)
            outs = [set(defined)]
            for case in node.cases:
                d = set(defined)
                for n in _walk_names(case.pattern):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        d.add(n.id)
                self.expr(case.guard, d, fn)
                outs.append(self.block(case.body, d, fn, depth))
            merged = outs[0]
            for o in outs[1:]:
                merged = merged & o
            return merged

        # 普通语句：先查读取，再记赋值
        self.expr(node, defined, fn)
        d = set(defined)
        for n in _walk_names(node, skip_nested=True):
            if isinstance(n.ctx, ast.Store):
                d.add(n.id)
            elif isinstance(n.ctx, ast.Del):
                d.discard(n.id)
        return d

    def block(self, body, defined, fn, depth=0):
        d = set(defined)
        for st in body:
            d = self.stmt(st, d, fn, depth + 1)
        return d

    def function(self, node):
        if node.name in getattr(self, "_seen", set()):
            return
        self._seen = getattr(self, "_seen", set())
        self._seen.add((node.name, node.lineno))
        a = node.args
        defined = set()
        for arg in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)):
            defined.add(arg.arg)
        if a.vararg:
            defined.add(a.vararg.arg)
        if a.kwarg:
            defined.add(a.kwarg.arg)
        if self.name in defined:
            return
        self.block(node.body, defined, node.name)


def scan_file(path, name, problems):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
        tree = ast.parse(src)
    except (SyntaxError, OSError):
        return
    an = Analyzer(name, path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            an.function(node)
    # 模块级
    top = set()
    fake = Analyzer(name, path)
    for st in tree.body:
        top = fake.stmt(st, top, "<module>")
    problems.extend(an.problems)
    problems.extend(fake.problems)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "status"
    roots = sys.argv[2:] or ["."]
    problems = []
    for root in roots:
        for dp, dns, fs in os.walk(root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for f in fs:
                if f.endswith(".py"):
                    scan_file(os.path.join(dp, f), name, problems)
    seen = set()
    for p in problems:
        k = (p[0], p[2])
        if k in seen:
            continue
        seen.add(k)
        print(f"{p[0]}:{p[2]}  in {p[1]}()   {p[3]}")
    print(f"--- {len(seen)} 处可能未绑定读取（{name}）")


if __name__ == "__main__":
    main()
