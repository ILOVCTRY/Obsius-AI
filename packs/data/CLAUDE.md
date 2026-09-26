# packs/data/

> 能力包侧**随包基线数据**：入库、随桌面包分发（scripts/build_exe.py 资源清单含本目录）。
> 消费代码在 `core/blackboard/cdn.py`；设计口径见 DESIGN.md §三「资产树归并、CDN 判定」。

## 文件

| 文件 | 用途 |
|------|------|
| `cdn_ranges.json` | **CDN/共享托管基线清单（asset-tree-derived-clean M1，2026-09-24）**：结构 `{version, updated, note, cidr[], cname_suffixes[]}`——`cidr`=CDN 专属 IP 段（当前 19 条，Cloudflare 等），`cname_suffixes`=CDN CNAME 后缀（25 条，如 `.cdn.dnsv1.com`）；解析命中即判 CDN：域名保持根行、不挂共享 IP、不并入资产树。**保守收录：只放确认的 CDN 专属地址/后缀**，拿不准默认非 CDN（宁误并不误拆） |

## 关键约定

- 本文件是 CDN 判定的基线真相源；**本地/私有 CDN 增补走 `config/cdn.json`**（gitignore，同结构），由 `load_cdn_lists` 合并，勿往基线里塞环境特定条目。
- **坏文件 fail-fast**：JSON 非法或字段类型错误 → `ValueError`（基线随包出错不静默吞）；增补文件缺失/损坏才容错跳过。
- 判定优先级在代码侧：资产 `meta.cdn` 人工覆盖（True/False）> CNAME 后缀 > IP CIDR。
- 仅放数据，不放可执行代码；更新清单后跑 `tests/test_cdn.py` + 全量回归。
