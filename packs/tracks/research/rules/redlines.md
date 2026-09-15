# 研究轨红线（永久强制，优先级最高）

> 与任何 skill / 知识库冲突时以本文件为准。

## 信任与执行

1. **样本默认 untrusted**：headless 反编译器（idat/analyzeHeadless）属可信**解析**工具，
   只解析不执行样本，可走 host；任何**执行**样本（含调试器动态分析）必须经人类明确授权，
   未知样本按 malware 纪律走 sandbox/断网（DESIGN.md §7）。
2. 不得为图方便把样本标 trusted 绕过隔离。

## 数据纪律

3. **三层数据不混淆**：全量客观函数在 headless 缓存（只读）；只有被分析过的函数才写
   func_kb（结论、改名、risk_tags），不把符号表灌进 func_kb。
4. **反编译前先查 func_kb**（按 binary_sha256 + 地址），重复分析是硬违规；分析完立即落库。
5. **发现挂样本**：findings 必须挂 binary 资产（target_asset_id），evidence 里带
   func_id/address 精确定位函数；结论分五类：algorithm/protocol/data-structure/mechanism/risk。
6. 无证据的结论标 unverified；人工确认或动态验证（带调试日志产物）才可标 verified。
7. **伪码不进事件流**：长伪码按需读缓存，不复制进事件/任务结论，避免淹没上下文。
