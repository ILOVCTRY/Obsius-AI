# -*- coding: utf-8 -*-
"""一次性：把 vuln-report-format.md 的 §二 判级表收成指针（2026-10-17 单一正本）。"""
import io

P = "packs/kb/web/实战打法/rules/vuln-report-format.md"
raw = io.open(P, encoding="utf-8", newline="").read()
crlf = "\r\n" in raw
lines = raw.replace("\r\n", "\n").split("\n")

start = next(i for i, l in enumerate(lines) if l.strip() == "### 等级判定")
end = next(i for i, l in enumerate(lines)
           if l.startswith("- **低敏业务数据"))
assert start < end, (start, end)

block = """### 等级判定（已收口到注入口径）

> **本表已删除（2026-10-17）**：档位条款只在 `安全红线/评级与价值口径/edu-rating.md` 维护，项目勾选 rating 后由 `rule:rating:edu-rating` **整篇注入 system 提示**。本节只留指针，不再重复条款——重复副本会与正本漂移（实测：旧表把「未授权读取课程正文」判成 high，正本为 low）。
>
> 判级一律读注入的 `rating:<tag>`；登记时 `rating_basis` 写 `rating:<tag> <档位>#<条号> <一句话依据>`，服务端会校验是否引用本项目生效口径。
> 本文件此后只负责**版式**与**落不落 / 匿名闸 / 认钥闸 / 清理纪律**（见下节）。""".split("\n")

lines[start:end + 1] = block
out = "\n".join(lines)
if crlf:
    out = out.replace("\n", "\r\n")
io.open(P, "w", encoding="utf-8", newline="").write(out)
print("replaced lines", start + 1, "-", end + 1, "crlf", crlf)
