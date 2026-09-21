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

## 逆向开发管线（R4 蓝图与重建，DESIGN.md §9 R4）

8. **仅限授权样本**：R4 管线只用于授权样本/自有软件/研究用途，非授权目标拒析；
   重建产物仅限研究与互操作，**禁分发**；禁用于绕过付费/授权/反作弊。
9. **生成代码一律 untrusted**：重建/自测产生的代码按不可信处理，run_cmd 只准
   docker/sandbox 容器执行（threat_class=untrusted），不得 host 直跑；V1 不执行原样本。
10. **划分纪律**：模块按业务职能聚类（网络通信/加密校验/文件持久化/许可校验/UI…），
    不按编译单元；已知库函数（libc/API 包装）标 noise 不进模块；每模块必附
    func_addresses 清单与业务推断理由；模块接口 spec（函数签名/数据结构/协议格式）
    先行钉死，后续深析与组装不得擅自变更接口——发现 spec 错了写 notes 上报，勿自行改。
11. **函数归属标签**：深析结论写 func_kb 时必须带 `module:<模块名>` risk_tag
    （蓝图模块归属的机器可读锚点），逐函数落库前先查 func_kb 防重复（第 4 条同此）。
12. **自测不过不得标 tested**：模块自测对拍（依 func_kb 伪码构造输入/输出向量，
    容器内跑自测脚本）全部通过才可置 module_status=tested；对拍不过就修到过或
    notes 记录失败原因上报，不得虚标。
