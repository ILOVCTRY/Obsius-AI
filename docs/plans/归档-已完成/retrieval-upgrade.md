# 方案：检索质量与上下文效率升级（skill+RAG 对标）

- **状态**：**已全量实施（M1+M2+M3+M4，2026-09-23）**（2026-09-22 四项方向用户拍板全做；M5 任务级评测基线按拍板后置至 independent-verification-audit M1 之后，不在本方案范围）
- **实施记录**（2026-09-23 夜间自主批次，待打磨 9 项拍板见 §3.1）：
  - **M1 缓存**：anthropic_compat system 块数组 + cache_control ephemeral 打标（stable 块末尾断点）+ 400 降级剥标重试；loop.py build_system_prompt 拆 stable/dynamic（规则链+角色+能力清单 / 技能指引+任务目标+纪律尾）；观测面 usage_view 补 cache_read/creation/hit + LiveRoom 预算弹窗命中率展示。**Ark 实测通过**：兼容层直接接受 cache_control（无需 provider 分支），同前缀第二跑 `cr=2176/in=54` 前缀缓存生效；降级路径保留作保险；
  - **M2 同义词表**：packs/kb/synonyms.yaml 全局单表 21 组；kbindex 查询侧扩展（命中组全成员切段并入，索引侧零改动）+ route_hints 挂载；doctor 体检（synonyms-missing/empty/dead-group/idle-term）；
  - **M3 对账+黄金集**：skill.routed payload 的 kb_hits 早前批次已落（**实施修正：方案 §4.3「前提补齐——hints 审计」一节作废**，loop.py 两分支 payload 均已带 kb_hits）；traces.retrieval_stats 三象限（hinted_opened / hinted_not_opened / opened_no_output / missed）+ `GET /projects/{pid}/retrieval-stats` 独立端点（拍板 #6）；黄金集 tests/fixtures/retrieval-golden.yaml 20 条（recall@4≥0.8 回归）+ tests/test_retrieval_golden.py（含 expect 路径存在自检）；
  - **M4 campaign**：切分复用 kbindex `_query_segments`（就地导入防环，中文 2-gram+停用词替代整段 LIKE）、字段加权 title 2.0 > 正文独有 1.0、轨/能力加成与热度 log1p×线性 30 天衰减保留（拍板 #7 沿用 experience-sedimentation M2 惯例）；黄金集 tests/fixtures/campaign-golden.yaml 6 条（mission 口吻 query→期望召回条目）+ tests/test_campaign_golden.py。
- **拍板记录**：§3（用户）/ §3.1（9 项待打磨实施拍板）
- **差距分析来源**：2026-09-22 与业界 skill/RAG 技术对照（Anthropic Agent Skills 渐进披露 / Agentic RAG / 混合检索 / RAGAS 评估 / prompt caching）——结论：架构方向（薄路由→提示行→kb_open 渐进披露 + Agent 主动检索）对齐前沿不落后，差距在工程细节四条
- **关联代码**：`core/llm/anthropic_compat.py`（system 块数组+cache_control+降级）、`core/agent/loop.py`（build_system_parts/build_system_blocks）、`core/autonomy.py`（usage_view 缓存字段）、`core/skills/kbindex.py`（同义词扩展 load_synonyms/synonym_expansion）、`core/skills/doctor.py`（synonyms-* 体检）、`core/blackboard/traces.py`（retrieval_stats）、`core/blackboard/campaign.py`（2-gram+字段加权召回）、`core/api/app.py`（retrieval-stats 端点）、`packs/kb/synonyms.yaml`、`tests/fixtures/retrieval-golden.yaml`、`tests/fixtures/campaign-golden.yaml`
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§六 运行时路由链路 / §七 LLM 接入层横切段），同步 `core/skills/CLAUDE.md` / `core/blackboard/CLAUDE.md` / `core/agent/CLAUDE.md`；本文保留作方案背景

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
| 4 | 任务级评测基线 | **M5 并入**（2026-09-22 第二批拍板，来源=斗象 100 小时 Agent 渗透战报对照）：黄金任务集 + 两口径评分卡，与 M3 检索层黄金集形成两层 |

### 3.1 待打磨清单实施拍板（2026-09-23 夜间自主批次，§5 九项逐条落定）

| # | 打磨项 | 实施拍板 |
|---|--------|----------|
| 1 | Ark 对 cache_control 容错实测 | **已实测通过**：Ark Anthropic 兼容层（ark.cn-beijing.volces.com/api/coding）直接接受 cache_control 块数组，同前缀第二跑 cache_read>0 前缀缓存生效；无需 provider 分支打标，降级路径（400 剥标重试+实例置位）保留作保险 |
| 2 | system 动态内容排查 | stable=规则链+角色块+能力清单（会话内字节稳定，实测钉死测试 test_system_blocks_dynamic_changes_keep_stable_prefix）；dynamic=技能指引+当前任务目标+纪律尾（每任务变）。拆块而非挪移：Anthropic 协议支持多 text 块，dynamic 块放 stable 断点之后 |
| 3 | SkillRouter keywords 挂同义词 | **暂不挂**——frontmatter keywords 已人工双语登记，挂同义词组误报面扩大；收益不明确前不动 |
| 4 | synonyms.yaml 格式细则 | 全局单表（不分能力域，组内 terms 自带域语义）+ lower 归一（子串匹配）+ 中文 2-gram 沿用 kbindex 切分，不单独做全半角归一（提案制演进，出现实际误配再补） |
| 5 | M3 hints 审计落点 | **前提过时作废**：skill.routed 两分支 payload 已带 kb_hits（早前批次已落，loop.py 认领/路由两处），无需任何补齐 |
| 6 | retrieval-stats 落点 | **独立端点** `GET /api/projects/{pid}/retrieval-stats`（traces.retrieval_stats 离线统计，窗口 5000 事件；不并入 trace-effect——口径不同不宜挤一个响应） |
| 7 | M4 时间衰减与 usage 权重 | 沿用 experience-sedimentation M2 的 log1p 惯例：`decay(线性30天) × log1p(usage) × 1.5`——热 10 次≈+3.6 vs 冷 1 次≈+1.1，马太被压平且近期条目仍优先 |
| 8 | 实施顺序 | M1→M2→M3→M4（缓存独立先行；词表见效快先上；评估建在其上度量增补收益；campaign 独立收尾） |
| 9 | M5 选题与判分依托 | **后置**：判分依托 independent-verification-audit M1 验证器，M5 移出本方案范围，随验证器排期 |

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

### 4.5 M5 任务级黄金集与两口径评分卡（e2e 评测基线）

- **定位**：M3 黄金集测「检索层召回」，M5 测「端到端解题能力」——固定靶题集跑版本间回归，度量每次大迭代的能力漂移（方法论来自 100 小时战报的口径纪律）；
- **黄金任务集**：`tests/fixtures/task-golden.yaml`——一小撮固定靶题起步（CTF 3-5 道 + 1-2 个 CVE 复现），每条带任务描述/靶环境说明/期望产出（finding 要点或 flag）；依赖 [independent-verification-audit.md](independent-verification-audit.md) M1 验证器做独立判分（无验证器前人工判）；
- **两口径评分卡**（照抄战报口径纪律）：**严格分母**（全集应做尽做通过率）与**实跑分母**（实际跑完验证的通过率）分列——混用即失真；难度分层（简单/困难）分段统计，不做单一总数；
- **跑法**：脚本化批跑（demo 三件套模式延伸），每次大迭代（如检索链改动/验证器上线/模型切换）跑一遍出对比表——能力变化有据可查。

## 5. 待打磨清单

> **（2026-09-23 已全部落定**，逐项拍板见 §3.1；下表保留作历史记录。）

1. M1 Ark 兼容层对 cache_control 未知字段容错实测（拒则 provider 分支打标）；
2. M1 system 动态内容清单排查与挪移方案（哪些块破前缀）；
3. M2 SkillRouter keywords 是否挂同义词组（收益 vs 误报面）；
4. M2 synonyms.yaml 格式细则（是否分能力域组 / 是否带置信级 / 大小写与全半角归一）；
5. M3 hints 审计落点（skill.routed payload 扩展 vs 新事件类型 kb.hints）；
6. M3 retrieval-stats 独立端点 vs 并入 trace-effect；
7. M4 时间衰减系数与 usage 权重平衡；
8. **实施顺序**：M1 独立先行无争议；M2/M3 谁先——M3 先建（评估先立，M2 增补有度量）vs M2 先行（词表见效快，M3 度量基线弱）——用户排期时定。
9. M5 黄金任务集选题与判分依托（独立判分依赖 independent-verification-audit M1 验证器上线程度，无验证器期先人工判）；靶环境可复现性（CVE 复现题的环境快照/部署脚本沉淀在哪）。

## 6. 实施切分建议（已按 M1→M2→M3→M4 实施，2026-09-23）

- **M1 缓存**：请求侧 cache_control + system 稳定性审计 + 观测展示（前端命中率 + 事件 payload）。
- **M2 同义词表**：synonyms.yaml + kbindex/route 查询扩展 + doctor 体检段。
- **M3 评估**：hints 审计落点 + retrieval-stats 对账 + retrieval-golden 黄金集回归。
- **M4 campaign**：2-gram 匹配 + 打分升级 + golden 断言。
- **M5 评测基线**：task-golden 黄金任务集 + 两口径评分卡脚本（判分依托 independent-verification-audit M1，建议排其后续期）。

依赖关系：M1 独立；M2/M3 互喂（评估发现盲区 → 词表增补 → 黄金集验证）；M4 独立；M5 弱依赖 M1 验证器（先人工判可起步）。
