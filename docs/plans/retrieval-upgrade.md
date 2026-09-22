# 方案：检索质量与上下文效率升级（skill+RAG 对标）

- **状态**：**讨论收敛，待打磨**（2026-09-22 四项方向用户拍板，全做；伪差距明确不追）
- **拍板记录**：§3
- **差距分析来源**：2026-09-22 与业界 skill/RAG 技术对照（Anthropic Agent Skills 渐进披露 / Agentic RAG / 混合检索 / RAGAS 评估 / prompt caching）——结论：架构方向（薄路由→提示行→kb_open 渐进披露 + Agent 主动检索）对齐前沿不落后，差距在工程细节四条
- **关联代码**：`core/llm/anthropic_compat.py`（请求组装/响应解析 :305-306 已提取 cache 字段）、`core/llm/provider.py`（LLMUsage cache_read/cache_creation_tokens）、`core/autonomy.py`（:167-174 record_llm_usage 已接缓存字段→usage_add_llm）、`core/skills/kbindex.py`（2-gram 标题+H2 段落索引/同义词挂载点）、`core/skills/router.py`（SkillRouter）、`core/blackboard/campaign.py`（整词召回 :100-138）、`core/agent/loop.py`（hints 组装 :659-690 **不落事件**）、`core/agent/tools.py`（kb_open/kb_search/route_lookup 工具面）、`packs/kb/`（全局单根）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§六 运行时路由链路 / §七 LLM 接入层横切段），同步 `core/skills/CLAUDE.md`；本文保留作方案背景

## 1. 背景与差距结论

对照业界得出的**四条真差距**（都做，即本方案）：

1. **Prompt caching 零利用**——每步重发稳定前缀（persona+规则链+技能正文+追加式历史），无缓存标记无收益；
2. **跨语言同义盲区**——2-gram/子串跨不过「越权↔IDOR」「反序列化↔deserialization」，双语覆盖靠分散人工维护且盲区不可见；
3. **漏召回不可见**——K7 zero-hit 只看假阳性（注入了不命中），假阴性（该提示没提示）完全盲；
4. **campaign 召回过于简陋**——整词命中 + usage_count 马太效应，召回质量直接影响编排决策。

**伪差距（明确不追，防军备竞赛）**：向量库/embedding（477 篇人工策划小语料，同义词表+Agent 改写已覆盖大半收益，嵌入服务依赖+索引维护+幻觉污染面不划算）、GraphRAG（黑板即知识图谱）、语义分块（手册是结构化人工文档，H2 段落定位已有）、cross-encoder 重排（候选 cap 4+Top-5 排序空间太小）、Mem0 式记忆（黑板事件溯源+状态机更强）。

## 2. 现状盘点（2026-09-22 核实）

- **缓存链路半通**：响应侧全通（anthropic_compat 解析 cache_read_input_tokens → LLMUsage → record_llm_usage → usage_add_llm 的 tokens_cache_read/creation 列）——**请求侧零 cache_control 标记**，字段恒 0，观测无展示面；
- **kbindex**：title+文件名+H2 段落标题+90 字摘要的 2-gram 索引，停用泛词表（`_STOP_GRAMS`），mtime 快检缓存，477 篇毫秒级；hints 组装进 system 提示（cap 4 条），**无审计落点**；
- **Agent 主动检索工具面齐全**：kb_open / kb_search / route_lookup——工具调用走 command/command.result 事件，可回溯；
- **campaign.recall**：query 整词命中 ×2.0/词 + track 匹配 +1.5 / capability +1.0 + `usage_count` ×0.5×decay——自注「简版不做 2-gram」；
- **效果榜（execution-trace-chain 已实施）**：(skill × kb 模块) × verified finding 正向统计——M3 的反向对账可复用同一数据源。

## 3. 定稿决策（用户拍板 2026-09-22）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 范围 | **四项全做**：M1 缓存 / M2 同义词表 / M3 漏召回评估 / M4 campaign 升级 |
| 2 | 技术路线 | 轻量自研延续：不上向量库不上外部检索服务；同义词表+评估闭环+缓存标记，全部现有架构内消化 |
| 3 | 伪差距 | 明确不追（§1 清单），后续讨论不再重复评估 |

## 4. 设计详述

### 4.1 M1 Prompt caching（core/llm）

- **请求侧打标**：anthropic_compat 请求组装——system 稳定大块末尾打 `cache_control: {"type": "ephemeral"}`（Anthropic 协议显式标记）。Ark OpenAI 兼容分支：doubao 前缀缓存自动生效（要求前缀稳定即可），**实测确认兼容层对未知字段容错**，拒收则按 provider 分支条件打标；
- **组装顺序稳定性审计**：排查 system 组装链（persona / 规则链 / 技能正文 / 提示行块 / 对话纪律）中的动态内容（时间戳、随机 id、随轮次变化变量）——破坏前缀稳定的内容挪到 messages 侧（消息区变化不影响 system 前缀命中）；
- **断点策略**：M1 只打 system 末一个断点；消息历史增量断点（倒数第 2 条附近）观察 system 档收益后再定；
- **观测面**：usage 记账已带缓存字段——补前端展示（命中率 = cache_read / (input + cache_read + cache_creation)）+ llm.usage 事件 payload 确认携带；预算逻辑不动（缓存 token 仍计数，只是便宜）；
- **验收**：同一会话连续步 cache_read > 0；对照步均输入成本下降可见。

### 4.2 M2 领域同义词表（kbindex 查询扩展）

- **落点**：`packs/kb/synonyms.yaml`（全局单表；kb 全局单根惯例，知识域配置进 packs，经提案制演进）；
- **格式草案**：

  ```yaml
  # 安全术语中英映射组：objective 命中组内任一成员 → 全组 2-gram 参与匹配（查询扩展）
  groups:
    - id: idor
      terms: [越权, 未授权访问, 水平越权, 垂直越权, IDOR, authorization-bypass, access-control]
    - id: deser
      terms: [反序列化, deserialization, unserialize, java反序列化, shiro]
  ```

- **匹配语义**：**查询侧扩展**——objective 的 2-gram 集合 ∪ 命中组全部成员的 2-gram 集合，索引侧零改动（477 篇不动），成本低无回滚风险；
- **挂载面**：kbindex 提示行 + route.json 任务导航两处；**SkillRouter keywords 暂不挂**（frontmatter keywords 已人工双语登记，挂了误报面扩大——待打磨 #3）；
- **维护**：人工起步；doctor 体检段（组内术语互相能命中对方文档——反向校验防死组）；M3 的 missed 清单人工审阅后转同义词增补（提案制）——评估与词表互喂。

### 4.3 M3 漏召回评估（missed-hints 对账 + 黄金集）

- **前提补齐——hints 审计**：hints/route 导航注入时不落事件，先补轻审计——**拟议 skill.routed payload 扩展 `kb_hints: [module...]`**（同一事件承载，不新增事件类型；skill.routed 本就含未命中场景，payload 一起带）——待打磨 #5；
- **对账口径（三象限，全部离线可算）**：对每个产出 verified finding 的任务——
  | 象限 | 信号 | 处置 |
  |---|---|---|
  | 提示了、没打开 | hints 给了 Agent 没 kb_open | 提示质量/摘要问题，低优 |
  | 打开了、没产出 | kb_open 了但任务失败/无发现 | 正常试错，不算 |
  | **没提示、打开且产出** | Agent 自己找到手册并打穿 | **missed 信号**：当时该提示未提示 → 审阅后增补同义词/keywords/route_index（提案制） |
- **承载**：`GET /projects/{pid}/retrieval-stats` 离线统计（数据源：skill.routed payload 的 kb_hints + command 事件里的 kb_open 调用 + finding 任务归属）——独立端点还是并入 trace-effect 扩展待打磨 #6；
- **黄金集**：`tests/fixtures/retrieval-golden.yaml`——常见中文任务描述 → 期望命中的 kb 模块（人工标注 30-50 条起步），pytest 回归断言三层匹配链（route.json → kbindex → 提示行）recall@4 阈值；**匹配逻辑任何改动跑黄金集防回归**，M2 词表增补的效果也用它度量。

### 4.4 M4 campaign 召回升级

- **2-gram 匹配**：复用 kbindex 的切分与停用词逻辑替代整词命中（「OA 系统」→「办公自动化平台」类跨写法命中）；
- **打分升级**：字段加权（mission/title > content 正文）+ **时间衰减**（近期打法优先）替代 usage_count 马太（usage 降为 log 衰减或移除——待打磨 #7）；
- **验证**：人工 golden 断言 5-10 条（mission 描述 → 期望召回的历史条目）进 pytest；
- **范围**：不引入向量/外部依赖（对齐拍板 #2）；recall 的 score 字段已透出（诊断用），打分变更可直接观察。

## 5. 待打磨清单

1. M1 Ark 兼容层对 cache_control 未知字段容错实测（拒则 provider 分支打标）；
2. M1 system 动态内容清单排查与挪移方案（哪些块破前缀）；
3. M2 SkillRouter keywords 是否挂同义词组（收益 vs 误报面）；
4. M2 synonyms.yaml 格式细则（是否分能力域组 / 是否带置信级 / 大小写与全半角归一）；
5. M3 hints 审计落点（skill.routed payload 扩展 vs 新事件类型 kb.hints）；
6. M3 retrieval-stats 独立端点 vs 并入 trace-effect；
7. M4 时间衰减系数与 usage 权重平衡；
8. **实施顺序**：M1 独立先行无争议；M2/M3 谁先——M3 先建（评估先立，M2 增补有度量）vs M2 先行（词表见效快，M3 度量基线弱）——用户排期时定。

## 6. 实施切分建议（待用户排期）

- **M1 缓存**：请求侧 cache_control + system 稳定性审计 + 观测展示（前端命中率 + 事件 payload）。
- **M2 同义词表**：synonyms.yaml + kbindex/route 查询扩展 + doctor 体检段。
- **M3 评估**：hints 审计落点 + retrieval-stats 对账 + retrieval-golden 黄金集回归。
- **M4 campaign**：2-gram 匹配 + 打分升级 + golden 断言。

依赖关系：M1 独立；M2/M3 互喂（评估发现盲区 → 词表增补 → 黄金集验证）；M4 独立。
