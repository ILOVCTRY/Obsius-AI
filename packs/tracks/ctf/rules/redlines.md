# CTF 领域红线（永久强制，优先级最高）

> 与任何 skill / 知识库冲突时以本文件为准。

## 分析纪律

1. **反编译前先查 func_kb**：分析任何函数前，必须 `bb_query what=func`（按 binary_sha256）
   看队友是否已分析过——重复劳动是硬违规；自己分析完立即 `bb_upsert_func` 落库。
2. **地址与证据绑定**：func_kb 的结论必须带地址；发现（findings）必须带证据
   （反编译片段 / 运行输出），无证据标 status=unverified。
3. **样本信任级不虚报**：CTF 附件默认 untrusted，不得为图方便标 trusted 绕过容器隔离。
   静态分析（strings/file/反汇编脚本读取）可用 host；执行样本必须 docker/sandbox。
4. **flag 反幻觉**：flag 只能来自实际运行输出或完整可复现的推导链，
   禁止"应该是这个"式猜测；推导所得标 unverified，跑通后补 verified。

## 时间纪律

5. 比赛场景下先拿分后完美：能出 flag 先出，优化分析留到赛后复盘任务。
