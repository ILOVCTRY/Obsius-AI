# tools/data/

> 工具链侧**随包数据资产**（toolchain-registry 的 `kind: "data"` 规范位）：入库、随桌面包分发。
> 在 registry.json 中以 `bin: {"*": "data/<名>"}` 声明，resolve 后给目录路径（见 tests/test_toolchain.py）。

## 目录

| 路径 | 用途 |
|------|------|
| `fpdb/fpdb_seed.json` | **本地指纹包首批（dsh asset-mapping 收编，2026-09-22，DESIGN §八）**：Web 资产指纹种子——结构 `{_comment, rules[]}`，规则 `{product, loc: body/title/header, kws[]（AND 语义，全部出现才命中）, source}`（83 条）+ `path_rules`（探测路径+确认词，16 条），签名均经红队实战项目验证。是 pentest 指纹三段式（FOFA 网络空间 → 本地指纹包 → 被动标题）的第二段 |

## 关键约定

- data 类只放数据不放代码；消费方经 registry `resolve_tool("fpdb")` 拿目录，勿硬编码相对路径。
- 种子是**精校小库**；全量库（5600+ 规则 / 4200+ 产品，来自 dsh asset-mapping）未来经 `scripts/fpdb_update.py` 重建（脚本尚未入库），届时规则仍保持 loc+kws/path_rules 同构。
- 扩充规则：kws 必须 AND 语义可落地、产品名用上游通用中文名；每条应能在授权靶场或实战流量验证后再入。
