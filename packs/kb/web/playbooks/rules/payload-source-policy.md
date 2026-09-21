# payload / 案例引用纪律（永久 · 唯一）

> 2026-09-11 融合定稿。本规则融合 src-hunter 反幻觉硬约束与 src-skill `skill-as-boost`
> 能力不封顶原则：**弹药查库优先，库无现场可造，证据纪律不减。**
> 与知识库 / references 冲突以本规则为准。

## 一、payload 来源三档

1. **查库优先**：要给 SQLi/RCE/SSRF/XSS/越权等任何 payload、或引用真实案例（H1/WooYun）前，
   先 Read 对应库文件：
   - 手法弹药 → `skills/src-strike/references/playbooks/<类型>.md`（目录式 playbook 先读 `00-index.md` 再按子文件路由定位，**不要把 00-index 当 payload 库**）
   - 真实案例 → `references/h1-reports/by-weakness/<weakness>.md`（说不出文件路径就不引用案例）
   - 指纹/默认口令 → `references/dictionaries/`
2. **库中没有 → 现场自造探针**：库没覆盖的参数/栈/场景，按 `src-value-hunting` §4 参数
   防漏矩阵现场构造，**不需要停下来报告用户**。
3. **标注义务**：报告与交付里，**现场构造**的 payload/手法在其 PoC 节标注一行
   `（现场构造，库无出处）`；查库命中的标出处文件相对路径。两种标注只选其一，一行即可。

## 二、证据纪律（不减）

- **无证据不下结论**：无 HTTP 包/截图/视频时只能写「待验证 / 假设」，不写「已确认 / 发现漏洞」。
- **不准编造案例编号**：引用 H1/WooYun 案例前必须 Read 实际文件。
- 报文判级、匿名闸、认钥闸仍只认 `vuln-report-format` §二；本规则不改变任何判级口径。

## 三、scope 硬闸（仅锁面生效）

- **锁面**（固定站 / URL 清单 / 众测 program）：任何时候发现要测的资产不在已确认 in-scope
  列表 → **立即停手**，回范围重核。核查期间禁止再向该资产发包。
- **自由跳**：归属判定走 `dig-scope-workflow`（种子/控股/归属链），禁回问、禁停工。

## 四、优先级

- 本规则压过知识库 / references 的任何「必须 Read 才能 payload」绝对条款（绝对条款降级为「查库优先」）。
- 不压过 `skill-as-boost`（能力不封顶）：现场构造不受本规则限制，只是要标注。
- 证据纪律条款与 `vuln-report-format`「敏感数据实读不是推测」叠加执行，冲突时取更严者。
