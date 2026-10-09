"""工具注册表（tool-registry，2026-10-03）。

**工具的唯一真相源**：name / description / input_schema / group / flags / handler
六项一处定义。此前这六项散落在 4-6 处（`AGENT_TOOLS` 静态表 + `TOOL_GROUPS` +
`_CONTROL_TOOLS`/`_PLAN_TOOLS`/`_INTENT_FLOW_TOOLS`/`_COLLAB_TOOLS`/`_KNOWLEDGE_EXTRA`/
`_FILE_TOOLS`/`_SPILL_SKIP` 各集合 + `agent_tool_group` 前缀规则 + `_tool_<name>`
命名约定），新增一个工具要同步改多处、漏一处就静默改变闸门放行面或落「其他」组。

**派生常量名全部保留**（`AGENT_TOOLS`/`TOOL_GROUPS`/`_CONTROL_TOOLS`/... 及
`agent_tool_group`），外部消费方（`core/api/app.py`、`core/agent/loop.py`、
`core/chat/runtime.py`）零改动。

**flags 语义**（每个 flag 对应迁移前的一个集合，判定口径逐条等价）：

| flag | 含义 |
|------|------|
| `control` | 控制原语，恒放行（`_CONTROL_TOOLS`） |
| `plan` | 计划原语，恒放行（`_PLAN_TOOLS`） |
| `plan_pre` | 计划闸放行面成员（`_PLAN_PRE_ALLOWED` 除 `plan` 外部分） |
| `intent_flow` | 意图流程工具，不算「意图声明后的执行证据」（`_INTENT_FLOW_TOOLS`） |
| `intent_pre` | 意图先行闸放行面成员（`_INTENT_PRE_ALLOWED` 除前三类外部分） |
| `spill_skip` | H1 spill 豁免（正文即取用目的，`_SPILL_SKIP`） |
| `collab` | 协作组（`_COLLAB_TOOLS`） |
| `knowledge` | 知识组（`_KNOWLEDGE_EXTRA`） |
| `file` | 文件组（`_FILE_TOOLS`） |

**改动纪律**：新增工具只改本文件一处（`tool(...)` 一段），`tools.py` 里补
`_tool_<name>` 实现即可——导入期自检会校验 handler 存在（见 `tools.py` 末尾），
group 必须落在 `TOOL_GROUPS` 内（`tool()` 装饰器当场拒绝），
`tests/test_tool_registry.py` 另钉死 flags 推导的常量与迁移前快照逐集合相等。
"""

from dataclasses import dataclass


_ADDR_DESC = "函数地址，hex 字符串优先（如 \"0x401000\"）；十进制整数也收"


TOOL_GROUPS = ["执行", "文件", "黑板", "知识", "浏览器", "协作", "计划", "控制"]


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的元数据。handler 缺省 = `_tool_<name>`（命名约定保留，便于阅读）。"""

    name: str
    description: str
    input_schema: dict
    group: str
    flags: frozenset = frozenset()
    handler: str = ""

    def as_schema(self) -> dict:
        """Anthropic 工具 schema（原 AGENT_TOOLS 的元素形态）。"""
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


REGISTRY: dict = {}


def tool(name, *, description, input_schema, group, flags=(), handler=""):
    """注册一个工具（导入期执行）。group 非法/重名当场抛错，杜绝静默落「其他」。"""
    if name in REGISTRY:
        raise ValueError(f"工具重复注册: {name}")
    if group not in TOOL_GROUPS:
        raise ValueError(f"工具 {name} 的 group 非法: {group!r}（须属 TOOL_GROUPS）")
    spec = ToolSpec(name=name, description=description, input_schema=input_schema,
                    group=group, flags=frozenset(flags),
                    handler=handler or f"_tool_{name}")
    REGISTRY[name] = spec
    return spec


# ---------------- 工具定义（顺序 = 迁移前 AGENT_TOOLS 顺序） ----------------

tool(
    'run_cmd',
    group='执行',
    flags=(),
    description=(
        '执行命令。runtime 按威胁等级选择：docker=Linux 渗透工具箱（bash + nmap/sqlmap/dirsearch/ffuf/py'
        'thon3 全套，workspace 挂载 /workspace；能力清单显示 pentest-box 镜像就绪时渗透/扫描/文本命令首选，未就绪回退 '
        'wsl）；host=Windows 特调（PowerShell，仅 Windows 目标/命令）；wsl=bash 兜底；sandbox=恶意样本（唯一'
        '选择，威胁等级不得虚报为 trusted）。落盘纪律见系统提示工具纪律。输出上限 2000 字符，超出截断并标注——读大文件优先用 read_file '
        '工具，或 run_cmd 用 grep -n 定位后小窗口（≤100 行）分段。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'cmd': {
                'type': 'string',
                'description': '要执行的命令',
            },
            'runtime': {
                'type': 'string',
                'enum': [
                    'host',
                    'wsl',
                    'docker',
                    'sandbox',
                ],
                'description': '可省略：省略时按本任务默认运行时（任务 chip 设的 preferred_runtime）执行；任务未设默认时必须显式传',
            },
            'threat_class': {
                'type': 'string',
                'enum': [
                    'trusted',
                    'untrusted',
                    'malware_live',
                ],
            },
            'net': {
                'type': 'string',
                'enum': [
                    'none',
                    'bridge',
                    'real',
                ],
                'description': '仅 sandbox/docker 有效；省略时 sandbox 默认 none、其余默认 bridge。real=真实网络，可直接指定（2026-10-01 起不再需要审批）',
            },
            'approval_id': {
                'type': 'string',
            },
            'timeout': {
                'type': 'number',
                'description': '超时秒数（可选）',
            },
        },
        'required': [
            'cmd',
            'threat_class',
        ],
    },
)

tool(
    'read_file',
    group='文件',
    flags=("plan_pre", "file"),
    description=(
        '只读工作区文件（2026-09-20 新增）：host 原生直接读，不经 WSL/PowerShell（无命令执行面、无启动开销、无引号转义问题）。pa'
        'th 相对 scratch（与 run_cmd cwd 一致）；返回带行号文本，默认读 100 行、单次上限 400 行；offset=-N 读末尾 N'
        ' 行。大文件先用 search_files 按内容定位再按行段读，不要盲猜行号。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': '相对 scratch 的路径（如 logs/run.log）；也收工作区内绝对路径',
            },
            'offset': {
                'type': 'integer',
                'description': '起始行号（1 起）；负数 N=读末尾 N 行',
            },
            'limit': {
                'type': 'integer',
                'description': '读取行数，缺省 100，上限 400',
            },
        },
        'required': [
            'path',
        ],
    },
)

tool(
    'search_files',
    group='文件',
    flags=("plan_pre", "file"),
    description=(
        '在工作区文本文件中按内容检索（2026-09-24 新增）：纯 Python 实现、不经 shell（无命令执行面、无注入风险），计划前也允许调（等价只'
        '读侦察）。默认搜 scratch 及其余工作区目录；只搜本项目工作区内文件，命中行以「相对路径:行号:内容」返回。适合在 bb_query 溢出落盘文件'
        '里定位关键词，替代 run_cmd grep。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'pattern': {
                'type': 'string',
                'description': '检索模式；regex=true（默认）时为 Python 正则，否则为普通子串（大小写不敏感）',
            },
            'path': {
                'type': 'string',
                'description': '检索范围目录/文件，相对 scratch（如 ../spill）；缺省=整个工作区；也收工作区内绝对路径',
            },
            'regex': {
                'type': 'boolean',
                'description': 'pattern 是否按正则解释，缺省 true；false=子串',
            },
            'glob': {
                'type': 'string',
                'description': '可选文件名通配（如 "*.txt"），缺省不限',
            },
            'max_results': {
                'type': 'integer',
                'description': '最多返回命中行数，缺省 100，上限 300',
            },
        },
        'required': [
            'pattern',
        ],
    },
)

tool(
    'bb_add_asset',
    group='黑板',
    flags=("intent_pre",),
    description=(
        '登记资产（host/domain/service/url/binary；type 省略按值自动识别，识别不出拒收回填）。资产树自动挂载：url/serv'
        'ice 含 IP 自动建 host，domain 自动 DNS 解析挂 host。重报同值=合并 meta 不插重复行（发布前先 bb_query 查重'
        '，勿重复登记）。meta.title=一句话简述；meta.owner=平台标签（.edu.cn 系→edusrc 等，命中的平台规则会注入会话）。扫描'
        '/测试状态走 bb_asset_status，不要写 meta.scanned。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'type': {
                'type': 'string',
                'description': 'host/domain/service/url/binary；省略或 auto=自动识别',
            },
            'value': {
                'type': 'string',
            },
            'meta': {
                'type': 'object',
                'description': 'title=一句话简述；owner=平台标签',
            },
            'parent_id': {
                'type': 'string',
                'description': '父资产 id（url/service 的 IP 主机部自动挂载，domain 自动 DNS；显式传可跳过自动逻辑）',
            },
        },
        'required': [
            'value',
        ],
    },
)

tool(
    'bb_delete_asset',
    group='黑板',
    flags=("intent_flow",),
    description=(
        '删除黑板中的非 binary 叶子资产。执行前必须用 bb_query what=assets 确认 asset_id；有子资产、被发现引用或是 bin'
        'ary 样本时会拒绝。binary 样本请走样本删除流程，发现引用请先修订/删除发现。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'asset_id': {
                'type': 'string',
                'description': '资产 id',
            },
        },
        'required': [
            'asset_id',
        ],
    },
)

tool(
    'bb_merge_assets',
    group='黑板',
    flags=("intent_flow",),
    description=(
        '将两个确认为同一实体的非 binary 资产合并。保留 target_asset_id，把 source_asset_id 的发现、意图锚点和子资产迁移'
        '到目标，并把源资产保存为目标 meta.aliases 后删除源行。合并不可逆，必须先 bb_query 查清两个资产并在 reason 中写明判断依据'
        '；跨项目、树结构不安全、发现去重键冲突或子资产重复时会拒绝。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'source_asset_id': {
                'type': 'string',
                'description': '被合并并删除的源资产 id',
            },
            'target_asset_id': {
                'type': 'string',
                'description': '保留的目标资产 id',
            },
            'reason': {
                'type': 'string',
                'description': '认定两者为同一资产的证据或理由',
            },
        },
        'required': [
            'source_asset_id',
            'target_asset_id',
            'reason',
        ],
    },
)

tool(
    'bb_asset_status',
    group='黑板',
    flags=(),
    description=(
        '流转资产扫描/测试状态（open→visited→scanning→tested_clean/budget_stop/na）。访问≠测试：访问过标 vi'
        'sited、开始扫描标 scanning、测完且无发现才标 tested_clean；预算/配额用尽被迫停手标 budget_stop、确认不适用（如非'
        '目标协议/离线主机）标 na——三者一样必须带 note（服务端强制），让「哪里没挖完、为什么」可对账。tested_clean 强制四问+逐资产背书（'
        '2026-09-29）：①tested_what 你对该资产自己测了什么（具体动作清单：路径/方法/响应特征）——「与同模板/基线一致」不等于已测试；②'
        'viewpoint 探测视角（docker 出口/直连/浏览器），遇 WAF/WebVPN 拦截页（如 488/403）必须说明如何排除是出口假象；③w'
        'hy_no_finding 为何是「无发现」而非「没测到」，还剩什么可立的新意图；④须有直接围绕该资产的 closed/dead_end 意图背书（ta'
        'rget 或 basis_refs 明确含本资产 id）——每资产独立立意意图，禁止父节点一条意图批量覆盖子树，意图越多测得越全面。四问答案并入审计事件'
        '可事后对账。测出问题直接 bb_add_finding，不要自报状态。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'asset_id': {
                'type': 'string',
            },
            'status': {
                'type': 'string',
                'enum': [
                    'visited',
                    'scanning',
                    'tested_clean',
                    'open',
                    'budget_stop',
                    'na',
                ],
            },
            'note': {
                'type': 'string',
                'description': 'tested_clean/budget_stop/na 必填：测了什么/为什么停/为什么不适用',
            },
            'tested_what': {
                'type': 'string',
                'description': 'tested_clean 必填（四问①）：你对该资产自己测了什么——具体动作清单（路径/方法/响应特征），同模板/基线一致不算测试',
            },
            'viewpoint': {
                'type': 'string',
                'description': 'tested_clean 必填（四问②）：探测视角（docker 出口/直连/浏览器）；遇拦截页（WAF/WebVPN 488/403）须说明排除出口假象的依据',
            },
            'why_no_finding': {
                'type': 'string',
                'description': 'tested_clean 必填（四问③）：为何是「无发现」而非「没测到」；还剩什么可立的新意图（收尾判据=没有可立的新意图）',
            },
            'expected_revision': {
                'type': 'integer',
                'description': '乐观锁：bb_query 读到的 rev 值。多窗同时改同一资产时防覆盖，冲突回 [冲突]',
            },
        },
        'required': [
            'asset_id',
            'status',
        ],
    },
)

tool(
    'bb_add_finding',
    group='黑板',
    flags=("intent_flow",),
    description=(
        '登记发现。复现步骤用 evidence.repro_steps=[{desc, type: http|python|cmd|image, code, e'
        'xpected, artifact_id?, stability?, target?}]——desc/expected 必填，code 填 HTTP 原'
        '始报文/python 脚本/bash 命令（type=image 时 artifact_id 必填指向图片产物、无 code），多步利用链按数组顺序；渗'
        '透/红队轨 verified 门禁=至少一步 code（或 artifact_id）非空且该步 expected 非空（旧结构 evidence.poc'
        '/pocs/poc_artifact_id 兼容但已 legacy）。危害描述 impact（影响事实：拿到什么/影响面）与修复建议 remediati'
        'on（可落地）直接落字段，报告按三件套渲染。无证据 status=unverified（无证据不下结论，红线见规则段）；**边干边写**——执行中每确认'
        '一条认知（端口/版本/未授权状态/接口行为/凭据线索等观察）立即以 category=intel + status=unverified 落一条，抗中断'
        '、抗上下文压缩、跨意图可复用；close_intent 收尾时再升 verified 或随死路一并了结，别攒到最后补记；注入评级口径（rule:rati'
        'ng:*）时 severity 必须按口径判级并填 rating_basis（见评级硬指令段）。CTF 轨语义：severity=线索级别（critic'
        'al=关键突破/high=可行动线索/其余=背景备查），vuln_class=线索类别；false-positive=死路（evidence 写清原因与'
        '已尝试清单，防重走弯路）。category 缺省自动判定（提示类或 info→intel，其余→vuln）。解题脚本/writeup 用 bb_add_'
        'artifact 落产物。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'vuln_class': {
                'type': 'string',
            },
            'title': {
                'type': 'string',
            },
            'severity': {
                'type': 'string',
                'enum': [
                    'info',
                    'low',
                    'medium',
                    'high',
                    'critical',
                ],
            },
            'category': {
                'type': 'string',
                'enum': [
                    'vuln',
                    'intel',
                ],
                'description': '缺省自动判定（提示类/info→intel，其余→vuln）；显式传参覆盖',
            },
            'status': {
                'type': 'string',
                'enum': [
                    'unverified',
                    'verified',
                    'false-positive',
                ],
            },
            'target_asset_id': {
                'type': 'string',
                'description': '挂到目标资产 id（URL 先 bb_add_asset 拿 id）',
            },
            'evidence': {
                'type': 'object',
                'description': (
                    '证据对象；复现步骤 repro_steps=[{desc,type,code,expected,…}] 是 verified 门禁认可结构（desc/e'
                    'xpected 必填，多步链按数组顺序）；旧 poc/pocs 字段 legacy 兼容，勿再新写'
                ),
            },
            'relates_to': {
                'type': 'array',
                'description': '强相关发现（攻击链强边）：[{finding_id, note}]，note 必填关联理由（如「同一注入点升级到 DBA」）；被引用发现必须已登记在本项目，否则 422；弱相关不要填',
                'items': {
                    'type': 'object',
                    'properties': {
                        'finding_id': {
                            'type': 'string',
                        },
                        'note': {
                            'type': 'string',
                        },
                    },
                    'required': [
                        'finding_id',
                        'note',
                    ],
                },
            },
            'poc_artifact_id': {
                'type': 'string',
                'description': 'Python 脚本 POC 的产物 id（bb_add_artifact 返回；legacy，复现步骤优先写 repro_steps+步骤内 artifact_id）',
            },
            'impact': {
                'type': 'string',
                'description': '危害描述（报告三件套）：影响事实——实际拿到什么数据/权限、影响面多大',
            },
            'remediation': {
                'type': 'string',
                'description': '修复建议（报告三件套）：可落地的修复措施',
            },
            'summary': {
                'type': 'string',
                'description': '漏洞摘要：漏洞是什么及触发条件',
            },
            'affected_assets': {
                'type': 'string',
                'description': '完整 URL/API、版本、业务模块',
            },
            'test_environment': {
                'type': 'string',
                'description': '操作系统、浏览器、工具版本、账号权限',
            },
            'reproduction_steps': {
                'type': 'string',
                'description': '审核人员可独立执行的操作步骤',
            },
            'verification_result': {
                'type': 'string',
                'description': '预期结果与实际结果',
            },
            'risk_assessment': {
                'type': 'string',
                'description': '攻击行为、影响范围及机密性/完整性/可用性后果',
            },
            'pocs': {
                'type': 'array',
                'description': '内嵌可复制 POC；每项仅 {type:http|python, code}',
                'items': {
                    'type': 'object',
                    'properties': {
                        'type': {
                            'type': 'string',
                            'enum': [
                                'http',
                                'python',
                            ],
                        },
                        'code': {
                            'type': 'string',
                        },
                    },
                    'required': [
                        'type',
                        'code',
                    ],
                },
            },
            'rating_basis': {
                'type': 'string',
                'description': '判级依据（F11）：注入评级口径时必填，格式「规则名+条款+一句话依据」，如「rating:edu-rating 高危#2 任意文件覆盖写」',
            },
            'confidence': {
                'type': 'number',
            },
            'dedup_key': {
                'type': 'string',
            },
        },
        'required': [
            'vuln_class',
            'title',
        ],
    },
)

tool(
    'bb_update_finding',
    group='黑板',
    flags=("intent_flow",),
    description=(
        '修订已有发现（只传要改的字段）。① 降级时 rating_basis 必须同给「规则名+条款+一句话依据」，否则宁可不改；② 口径外内容（纯暴露面/过期'
        '组件等）→ category=intel + severity=low 转有效线索，不要删除；③ 误报/死路 → status=false-positi'
        've（触发撤回传播通知引用方）；④ evidence 浅层合并（键级覆盖，列表键整键替换——补复现步骤请整组传 repro_steps 全量）。rati'
        'ng_basis/impact/remediation 不传=不动，传空串=清空。渗透/红队轨不收 severity=info（服务端拒收，CTF 轨可'
        '用）。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'finding_id': {
                'type': 'string',
            },
            'severity': {
                'type': 'string',
                'enum': [
                    'info',
                    'low',
                    'medium',
                    'high',
                    'critical',
                ],
            },
            'status': {
                'type': 'string',
                'enum': [
                    'unverified',
                    'verified',
                    'false-positive',
                ],
            },
            'title': {
                'type': 'string',
            },
            'vuln_class': {
                'type': 'string',
            },
            'category': {
                'type': 'string',
                'enum': [
                    'vuln',
                    'intel',
                ],
                'description': 'vuln=漏洞 / intel=有效发现·线索',
            },
            'evidence': {
                'type': 'object',
                'description': '浅层合并：键级覆盖、未传键不动；repro_steps 为列表键=整键替换，修订步骤须传全量数组',
            },
            'rating_basis': {
                'type': 'string',
                'description': '判级依据：改 severity 时必填；空串=清空，不传=不动',
            },
            'impact': {
                'type': 'string',
                'description': '危害描述（报告三件套）：空串=清空，不传=不动',
            },
            'remediation': {
                'type': 'string',
                'description': '修复建议（报告三件套）：空串=清空，不传=不动',
            },
            'summary': {
                'type': 'string',
            },
            'affected_assets': {
                'type': 'string',
            },
            'test_environment': {
                'type': 'string',
            },
            'reproduction_steps': {
                'type': 'string',
            },
            'verification_result': {
                'type': 'string',
            },
            'risk_assessment': {
                'type': 'string',
            },
            'pocs': {
                'type': 'array',
                'description': '全量替换；每项 {type:http|python, code}',
                'items': {
                    'type': 'object',
                    'properties': {
                        'type': {
                            'type': 'string',
                            'enum': [
                                'http',
                                'python',
                            ],
                        },
                        'code': {
                            'type': 'string',
                        },
                    },
                    'required': [
                        'type',
                        'code',
                    ],
                },
            },
            'expected_revision': {
                'type': 'integer',
                'description': '乐观锁：bb_query 读到的 rev 值。多窗同时改同一发现时防覆盖，冲突回 [冲突]',
            },
        },
        'required': [
            'finding_id',
        ],
    },
)

tool(
    'bb_delete_finding',
    group='黑板',
    flags=("intent_flow",),
    description=(
        '物理删除发现（按 finding_id，reason 必填——删错不可恢复）。仅限垃圾/走查数据（重复噪声、无价值记录）；**verified 发现不可'
        '删除**；误报走 bb_update_finding 改 status=false-positive；口径外内容（暴露面等）走 bb_update_fi'
        'nding 转 intel 线索而非删除。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'finding_id': {
                'type': 'string',
            },
            'reason': {
                'type': 'string',
                'description': '删除原因（审计用，必填）',
            },
        },
        'required': [
            'finding_id',
            'reason',
        ],
    },
)

tool(
    'bb_add_artifact',
    group='黑板',
    flags=(),
    description=(
        "落产物文件（POC 脚本/抓包/输出）：写项目产物目录 + sha256 落库。kind='poc' 仅限 .py——报文打不稳的漏洞才用脚本复现。ki"
        "nd='project' 限 R4 组装集成最终交付（整个重建程序的 zip 包）。CTF：解题/复现脚本与 writeup 必须落 artifact，"
        "不写裸文件（队友与续跑会话靠黑板找现场）。"
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'filename': {
                'type': 'string',
                'description': '文件名（纯文件名，不含路径；kind=poc 时必须 .py）',
            },
            'content': {
                'type': 'string',
                'description': '文件全文',
            },
            'kind': {
                'type': 'string',
                'enum': [
                    'file',
                    'poc',
                    'capture',
                    'project',
                ],
            },
            'description': {
                'type': 'string',
            },
        },
        'required': [
            'filename',
            'content',
        ],
    },
)

tool(
    'bb_query',
    group='黑板',
    flags=("plan_pre",),
    description=(
        '查黑板：findings / assets / events / func（函数知识库）/blueprint（开发蓝图）/ site（单'
        '站全貌）。**按域名/站点查现状（资产+发现+意图）一律用 what=site 并传 asset**（一次返回根子树全貌）——**不要**用 what='
        'assets 当站点查询：assets 是资产清单面（列清单/按 type/status/tag 筛），其 asset 参数只做子树过滤、不带 find'
        'ings/intents。不要按 type 分片拉全量再本地过滤（host/url/domain 各拉一把、条数多还漏看）。findings 尽量带 t'
        'arget_asset_id 精确过滤；events 尽量带 kinds/session_id；limit 传小值会截断漏看（漏看了仍要重查，得不偿失）'
        '。events 缺省=本项目**全部 kind**（不是只有 chat.* 会话事件），要窄看再传 kinds 白名单；**负结论（未发现/死路）不要硬'
        '塞进 events**：正确做法是 declare_intent 声明假设，再 close_intent outcome=dead_end（附 dead'
        '_reason+evidence_refs）——情报类非漏洞结论则用 bb_add_finding category=intel（severity>=l'
        'ow）。逆向场景硬规则：反编译任何函数前必须先查 func 防重复劳动。**列已上传样本：what=assets type=binary**——返回行的'
        ' value 即样本 sha256（func 查询的 binary_sha256 / bb_upsert_func 都用它），meta.filename'
        ' 是原文件名；不知道 sha 时先列样本，不要猜。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'what': {
                'type': 'string',
                'enum': [
                    'findings',
                    'assets',
                    'events',
                    'func',
                    'blueprint',
                    'site',
                ],
            },
            'asset': {
                'type': 'string',
                'description': (
                    '按目标筛资产（id 或精确 value，host/domain）。what=site：必填，返回子树资产+发现+意图（站点全景）；what=assets'
                    '：可选，只回该资产**子树内**的资产清单（不带发现/意图）——按域名查现状优先用 site'
                ),
            },
            'blueprint_id': {
                'type': 'string',
                'description': 'blueprint 查询：单份蓝图（缺省列全部）',
            },
            'target_asset_id': {
                'type': 'string',
                'description': 'findings：只回挂在该资产上的发现（精确匹配；查站点全貌请改用 what=site）',
            },
            'binary_sha256': {
                'type': 'string',
                'description': 'func 查询必填',
            },
            'address': {
                'type': [
                    'integer',
                    'string',
                ],
                'description': f"func 单点查询。{_ADDR_DESC}",
            },
            'risk_tag': {
                'type': 'string',
                'description': 'func 按风险标签过滤',
            },
            'type': {
                'type': 'string',
                'description': 'assets 按类型过滤（host/domain/service/url/binary）',
            },
            'verbose': {
                'type': 'boolean',
                'description': 'assets：true=返回 meta 全文字段（默认只回关键 meta，防大体积）',
            },
            'status': {
                'type': 'string',
                'description': (
                    '按状态过滤（语义随 what）：assets=open/visited/scanning/tested_clean/budget_stop/na（并发会'
                    '话可借此感知哪些目标正被扫）'
                ),
            },
            'min_severity': {
                'type': 'string',
                'enum': [
                    'info',
                    'low',
                    'medium',
                    'high',
                    'critical',
                ],
                'description': 'findings：不低于该严重级（high → high+critical）',
            },
            'verified_only': {
                'type': 'boolean',
                'description': 'findings：只回已验证（status=verified）',
            },
            'category': {
                'type': 'string',
                'description': 'findings：按类别过滤（vuln=漏洞 / intel=有效发现·关键发现）',
            },
            'tag': {
                'type': 'string',
                'description': 'assets：按 meta.tags 标签过滤（大小写不敏感）',
            },
            'kinds': {
                'type': 'array',
                'items': {
                    'type': 'string',
                },
                'description': 'events：只回这些事件类型（如 ["command","finding.new"]）',
            },
            'session_id': {
                'type': 'string',
                'description': 'events：只回该会话落的事件',
            },
            'limit': {
                'type': 'integer',
                'description': '返回条数上限，1-200（events 默认 50；findings/assets/func/blueprint 默认全量）',
            },
        },
        'required': [
            'what',
        ],
    },
)

tool(
    'kb_open',
    group='知识',
    flags=("plan_pre", "spill_skip", "knowledge"),
    description=(
        '[legacy] 打开尚未迁移的全局知识库模块（packs/kb/ 区，含测试包手册与快照）：返回文件绝对路径，随后按需 Read。.md/.py/.t'
        'xt/.json 均可打开（弹药脚本只是文本，执行仍须走 run_cmd）。路径不存在会返回可用模块清单（照清单改选，禁止猜文件名、禁止 .. 穿越）。'
        '禁止通读知识库目录。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'module': {
                'type': 'string',
                'description': '知识库内相对路径，如 webapp/idor/手册.md（web 测试包）、refs/ctf-pwn/heap-fsop.md（快照原件）',
            },
        },
        'required': [
            'module',
        ],
    },
)

tool(
    'kb_search',
    group='知识',
    flags=("plan_pre", "knowledge"),
    description=(
        '[legacy] 按关键词全文检索尚未迁移的全局知识库（大小写不敏感子串匹配）：返回 {path（即 kb_open 的 module 参数）, sou'
        'rce, matches, snippet}。多关键词空格分隔为 AND 语义（各词都命中的文件才返回），零结果自动放宽为 OR（任一词命中）。tag '
        '可选：按 frontmatter 分面标签过滤（phase/vuln_class，如 tag=jwt 只留打了该标签的手册）。典型用法：kb_searc'
        'h 定位 → kb_open 细读。中文专题用中文词、英文快照用英文词，必要时各试一次。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '检索关键词（子串匹配；空格分隔多关键词）',
            },
            'limit': {
                'type': 'integer',
                'description': '返回条数上限，默认 10，最大 50（可选）',
            },
            'tag': {
                'type': 'string',
                'description': '分面标签过滤（可选，phase/vuln_class 值，如 sqli、jwt、recon）',
            },
        },
        'required': [
            'query',
        ],
    },
)

tool(
    'propose_pack_edit',
    group='知识',
    flags=("intent_pre", "knowledge"),
    description=(
        '提一条知识库/技能/路由索引变更**提案**（只落 pending，批准权在人类，禁止自行改文件；允许的三种情形、reason 证据要求与沉淀去向见系统'
        '提示工具纪律）。每会话最多 3 条。kind=skill mode=edit（改正文，target={skill_kind: capability|tr'
        'ack, owner: 包/轨名, name: 技能名}）或 mode=suggest（粒度过粗的技能提「拆分建议」：content 为建议文档全文，批'
        '准后落技能目录 拆分建议.md，结构变更由人执行）；kind=kb mode 可为 edit/create/rename/delete，target={'
        'cap: 能力包名, path: 全局模块 路径 <域>/<快照>/<文件>（.md/.py/.txt/.json，中文目录允许）, new_path:'
        ' 仅 rename}；kind=case mode=edit|create（K6 成功链沉淀：本任务 verified 攻击链/跑通 payload 落'
        '对应测试包 成功案例.md 补段或 payloads/ 补弹药，target 同 kb）；kind=index mode=edit（增补全局 route'
        '_index.yaml 中本域条目，kb 路径须带域前缀），target={cap: 能力包名}——新增了值得索引的测试点手册时同步 route_ind'
        'ex.yaml（content 为本域条目全文）。edit/create 必带 content 全文。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'kind': {
                'type': 'string',
                'enum': [
                    'kb',
                    'skill',
                    'index',
                    'case',
                ],
            },
            'mode': {
                'type': 'string',
                'enum': [
                    'edit',
                    'create',
                    'rename',
                    'delete',
                    'suggest',
                ],
            },
            'target': {
                'type': 'object',
                'description': 'kb: {cap,path,new_path?}；skill: {skill_kind,owner,name}',
            },
            'content': {
                'type': 'string',
                'description': 'edit/create 的文件全文（其余模式不传）',
            },
            'summary': {
                'type': 'string',
                'description': '一句话摘要（≤300 字）',
            },
            'reason': {
                'type': 'string',
                'description': '为什么改 + 本任务证据（任务 id/关键观察）',
            },
        },
        'required': [
            'kind',
            'mode',
            'target',
            'summary',
            'reason',
        ],
    },
)

tool(
    'bb_upsert_func',
    group='黑板',
    flags=(),
    description='写函数知识库（逆向防重复劳动）：同地址重复写入自动合并（名称入演变史、analysis 追加、confidence 取最大）。分析完一个函数就落一条，别的会话才能看见。',
    input_schema={
        'type': 'object',
        'properties': {
            'binary_sha256': {
                'type': 'string',
            },
            'address': {
                'type': [
                    'integer',
                    'string',
                ],
                'description': f"{_ADDR_DESC}",
            },
            'name': {
                'type': 'string',
                'description': '你给函数起的名（含语义，如 check_flag）',
            },
            'analysis': {
                'type': 'string',
                'description': '行为/算法结论',
            },
            'risk_tags': {
                'type': 'array',
                'items': {
                    'type': 'string',
                },
            },
            'confidence': {
                'type': 'number',
            },
        },
        'required': [
            'binary_sha256',
            'address',
            'name',
        ],
    },
)

tool(
    'bb_blueprint_create',
    group='黑板',
    flags=(),
    description=(
        '建开发蓝图骨架（R4 逆向重建中枢）：模块划分任务的产出。modules 按业务职能聚类（网络通信/加密校验/文件持久化/许可校验…），不按编译单元；已'
        '知库函数（libc/API 包装）不进模块；每模块必附 func_addresses 清单与业务推断理由（desc）。模块接口约定（spec 雏形：函数'
        '签名/数据结构/协议格式）在此先行钉死——后续并行深析与组装全靠接口先行防冲突。同项目同样本下重名会拒收。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
            },
            'goal': {
                'type': 'string',
                'description': '重建目标：要造一个什么业务逻辑的程序',
            },
            'binary_sha256': {
                'type': 'string',
                'description': '目标样本 sha256（非单样本蓝图可空）',
            },
            'modules': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'name': {
                            'type': 'string',
                        },
                        'desc': {
                            'type': 'string',
                            'description': '业务职能与推断理由',
                        },
                        'func_addresses': {
                            'type': 'array',
                            'items': {
                                'type': 'string',
                            },
                            'description': 'hex 地址串',
                        },
                        'spec': {
                            'type': 'string',
                        },
                        'notes': {
                            'type': 'string',
                        },
                        'status': {
                            'type': 'string',
                            'enum': [
                                'pending',
                                'analyzed',
                                'specd',
                                'tested',
                            ],
                        },
                    },
                    'required': [
                        'name',
                    ],
                },
            },
        },
        'required': [
            'name',
        ],
    },
)

tool(
    'bb_blueprint_update',
    group='黑板',
    flags=(),
    description=(
        '更新开发蓝图（分区更新，别的会话实时可见）。三种用法：①模块深析写回：module_name + spec/notes/func_addresses/m'
        'odule_status（spec=接口约定：函数签名/数据结构/协议格式；自测不过不得标 module_status=tested）；②整表重划分：m'
        'odules_set 替换全部模块；③汇总正文：content_append 增量追加或 content_md 整体替换（数据流/接口表/算法/协议/状'
        '态机）。蓝图整体 status（draft/reviewed/ready/building/built）不归 Agent 管——reviewed/rea'
        'dy 由人类批准，勿申请。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'blueprint_id': {
                'type': 'string',
            },
            'module_name': {
                'type': 'string',
                'description': '模块级写回时必填（须为既有模块名）',
            },
            'spec': {
                'type': 'string',
                'description': '接口约定（函数签名/数据结构/协议格式）',
            },
            'notes': {
                'type': 'string',
                'description': '关键实现要点/业务逻辑发展',
            },
            'desc': {
                'type': 'string',
            },
            'func_addresses': {
                'type': 'array',
                'items': {
                    'type': 'string',
                },
            },
            'module_status': {
                'type': 'string',
                'enum': [
                    'pending',
                    'analyzed',
                    'specd',
                    'tested',
                ],
                'description': 'tested 必须容器自测通过后才可标',
            },
            'modules_set': {
                'type': 'array',
                'description': '整表替换模块数组（重划分，慎用）',
                'items': {
                    'type': 'object',
                },
            },
            'content_append': {
                'type': 'string',
                'description': '追加到蓝图正文末尾',
            },
            'content_md': {
                'type': 'string',
                'description': '整体替换蓝图正文',
            },
        },
        'required': [
            'blueprint_id',
        ],
    },
)

tool(
    'bb_logic_block_create',
    group='黑板',
    flags=(),
    description=(
        '建业务逻辑块（逆向理解笔记，与蓝图/攻击链并列的第三种载体）：记录函数协作如何构成业务功能——函数逻辑分析/业务逻辑分析/逆向破解/游戏业务理解的落点（'
        'PWN/漏洞利用登记走攻击链，重建管线走蓝图，不要混用）。一个块=一项可讲述的业务功能（如「存档校验」「金币结算」「协议握手」），funcs 挂构成该功'
        '能的函数与各自角色注（一句话职责）。address 必须已登记 func_kb（先 decompile/bb_upsert_func 再挂）；同项目同样'
        '本下块名重名会拒收。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                'description': '业务功能名（如「存档校验」）',
            },
            'binary_sha256': {
                'type': 'string',
                'description': '目标样本 sha256（挂函数的前提，必填）',
            },
            'description': {
                'type': 'string',
                'description': '业务逻辑描述（markdown）：触发时机/输入输出/状态流转/与其他块的关系',
            },
            'funcs': {
                'type': 'array',
                'description': '构成该功能的函数（可后续 bb_logic_block_update 增补）',
                'items': {
                    'type': 'object',
                    'properties': {
                        'address': {
                            'type': [
                                'integer',
                                'string',
                            ],
                            'description': 'hex 地址串或 int',
                        },
                        'role': {
                            'type': 'string',
                            'description': '该函数在本块中的职责一句话',
                        },
                    },
                },
            },
        },
        'required': [
            'name',
            'binary_sha256',
        ],
    },
)

tool(
    'bb_logic_block_update',
    group='黑板',
    flags=(),
    description=(
        '更新业务逻辑块（分区增量写）：description 整体替换业务描述；add_funcs 增挂函数；func_roles 修订既有挂接的角色注；rem'
        'ove_addresses 摘除函数。分析深入后回头补描述是预期工作流（先骨架后丰满）。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'block_id': {
                'type': 'string',
            },
            'description': {
                'type': 'string',
                'description': '整体替换业务描述（markdown）',
            },
            'add_funcs': {
                'type': 'array',
                'description': '增挂函数',
                'items': {
                    'type': 'object',
                    'properties': {
                        'address': {
                            'type': [
                                'integer',
                                'string',
                            ],
                        },
                        'role': {
                            'type': 'string',
                        },
                    },
                },
            },
            'func_roles': {
                'type': 'array',
                'description': '修订既有挂接的角色注',
                'items': {
                    'type': 'object',
                    'properties': {
                        'address': {
                            'type': [
                                'integer',
                                'string',
                            ],
                        },
                        'role': {
                            'type': 'string',
                        },
                    },
                },
            },
            'remove_addresses': {
                'type': 'array',
                'description': '摘除函数（hex 地址串）',
                'items': {
                    'type': 'string',
                },
            },
        },
        'required': [
            'block_id',
        ],
    },
)

tool(
    'decompile',
    group='知识',
    flags=("plan_pre", "knowledge"),
    description=(
        '反编译（Ghidra/IDA/MCP 自动选路，结果缓存）。硬规则：若该地址在 func_kb 已有分析结论，本工具直接返回缓存结论（禁止重复反编译）；'
        '拿到伪码后必须 bb_upsert_func 落库。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'binary': {
                'type': 'string',
                'description': '二进制文件路径',
            },
            'address': {
                'type': [
                    'integer',
                    'string',
                ],
                'description': f"函数入口地址；缺省=全量概览。{_ADDR_DESC}",
            },
            'name': {
                'type': 'string',
                'description': '按符号名匹配',
            },
        },
        'required': [
            'binary',
        ],
    },
)

tool(
    'list_symbols',
    group='知识',
    flags=("plan_pre", "knowledge"),
    description='列出二进制函数符号表（地址/名字/大小/是否已导出伪码）。支持按名/按大小过滤——找关键函数先用它缩小范围，勿整表吞。',
    input_schema={
        'type': 'object',
        'properties': {
            'binary': {
                'type': 'string',
            },
            'name_contains': {
                'type': 'string',
                'description': '按符号名子串过滤（大小写不敏感）',
            },
            'min_size': {
                'type': 'integer',
                'description': '只回大于等于该字节数的函数（过滤 stub/thunk）',
            },
        },
        'required': [
            'binary',
        ],
    },
)

tool(
    'strings_search',
    group='知识',
    flags=("plan_pre", "knowledge"),
    description='检索二进制字符串表（大小写不敏感子串），行带地址+引用函数。逆向找提示/密钥/flag 线索第一步用它，不要用 run_cmd 直读缓存 JSON。',
    input_schema={
        'type': 'object',
        'properties': {
            'binary': {
                'type': 'string',
                'description': '二进制文件路径',
            },
            'pattern': {
                'type': 'string',
                'description': '子串过滤（大小写不敏感）；缺省=全量（会被 limit 截断）',
            },
            'limit': {
                'type': 'integer',
                'description': '最多返回条数（默认 200，上限 1000）',
            },
        },
        'required': [
            'binary',
        ],
    },
)

tool(
    'func_xrefs',
    group='知识',
    flags=("plan_pre", "knowledge"),
    description='查函数调用关系（callers/callees）。给 name 或 address 其一；地址会先解析成函数名。判断关键函数被谁调/调了谁时用它。',
    input_schema={
        'type': 'object',
        'properties': {
            'binary': {
                'type': 'string',
                'description': '二进制文件路径',
            },
            'name': {
                'type': 'string',
                'description': '按符号名查',
            },
            'address': {
                'type': [
                    'integer',
                    'string',
                ],
                'description': f"按函数入口地址查。{_ADDR_DESC}",
            },
        },
        'required': [
            'binary',
        ],
    },
)

tool(
    'disasm',
    group='知识',
    flags=("plan_pre", "knowledge"),
    description='反汇编单函数（按需自动从 IDA MCP 拉取并落盘缓存，超长截断）。看汇编细节/混淆代码/指令级逻辑时用它；伪码看 decompile。给 name 或 address 其一。',
    input_schema={
        'type': 'object',
        'properties': {
            'binary': {
                'type': 'string',
                'description': '二进制文件路径',
            },
            'name': {
                'type': 'string',
                'description': '按符号名反汇编',
            },
            'address': {
                'type': [
                    'integer',
                    'string',
                ],
                'description': f"按函数入口地址反汇编。{_ADDR_DESC}",
            },
        },
        'required': [
            'binary',
        ],
    },
)

tool(
    'bb_notify',
    group='黑板',
    flags=("intent_pre",),
    description=(
        '私信其他会话窗（黑板异步协调，2026-09-20 会话窗对话化）：同步情报/移交工作/请求协助。to_session 定向单窗（优先），to_role'
        ' 广播该角色全部活跃窗（不含自己）；两者必须给一个。收件方在步边界/认领期/对话轮注入，不实时打断对方工作（异步模型，无同步对话接力）；不设频率硬限额，'
        '滥用在审计可见。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'to_session': {
                'type': 'string',
                'description': '收件会话 id（sess- 前缀，定向优先）',
            },
            'to_role': {
                'type': 'string',
                'description': '收件角色 id（广播该角色全部活跃窗；无 to_session 时生效）',
            },
            'kind': {
                'type': 'string',
                'enum': [
                    'intel',
                    'handoff',
                    'assist',
                ],
                'description': 'intel=情报同步 handoff=工作移交 assist=协助请求；缺省 intel',
            },
            'text': {
                'type': 'string',
                'description': '正文（≤4000 字符超出截断）；黑板对象一律引用 id（find-/as-/task-）',
            },
            'refs': {
                'type': 'array',
                'items': {
                    'type': 'string',
                },
                'description': '相关黑板对象 id 清单（可选）',
            },
        },
        'required': [
            'text',
        ],
    },
)

tool(
    'finish',
    group='控制',
    flags=("control", "intent_flow"),
    description='结束本次会话（提交最终总结）。任务未收尾时会被自动标记失败。',
    input_schema={
        'type': 'object',
        'properties': {
            'summary': {
                'type': 'string',
            },
        },
        'required': [
            'summary',
        ],
    },
)

tool(
    'request_steps',
    group='控制',
    flags=("control", "intent_flow"),
    description='申请步数预算增补（一次固定 +200）。仅当剩余步数 ≤20 时放行，剩余充足时会被拒收（防未雨绸缪囤步数）。步数耗尽会话会自动暂停等人类恢复，所以预算吃紧时请主动申请并说明理由。',
    input_schema={
        'type': 'object',
        'properties': {
            'reason': {
                'type': 'string',
                'description': '为何还需要更多步数（一句话，落审计）',
            },
        },
    },
)

tool(
    'browser_replay',
    group='浏览器',
    flags=(),
    description=('重放一条 HTTP 请求（F6 重放）：给原始报文 raw（Burp/DevTools/Yakit 复制的'
                 '请求行+头+体）或抓包记录 capture_id，二选一。结果入抓包历史（人类可在重放台查看）。'
                 '可经 proxy 走代理池做 IP 轮换（先起代理池服务，用其 http 入口）。'),
    input_schema={
        'type': 'object',
        'properties': {
            'raw': {'type': 'string', 'description': '原始请求报文；与 capture_id 二选一'},
            'capture_id': {'type': 'integer', 'description': '抓包历史行 id（模板）；与 raw 二选一'},
            'proxy': {'type': 'string', 'description': '显式代理（如 127.0.0.1:1801 代理池入口）；缺省直连'},
            'force_https': {'type': 'boolean', 'description': '请求行 http→https'},
            'follow_redirects': {'type': 'boolean', 'description': '跟随重定向（默认 true）'},
            'insecure': {'type': 'boolean', 'description': '跳过证书校验'},
            'gm_tls': {'type': 'boolean', 'description': '国密 TLS（走 gmhttp sidecar，需二进制就位）'},
            'timeout_s': {'type': 'number', 'description': '超时秒数（默认 15）'},
        },
    },
)

tool(
    'browser_intruder',
    group='浏览器',
    flags=(),
    description=('HTTP 爆破（F6 Intruder）：template 用 §名字§ 标出替换位（url/body），'
                 'payloads 给每个标记的取值集。结果逐请求入抓包历史（按 batch_id 拉取）。'
                 '并发/速率/总请求数服务端硬顶；可经 proxy 走代理池做 IP 轮换。'),
    input_schema={
        'type': 'object',
        'properties': {
            'template': {'type': 'object', 'description': '{method,url,headers,body}，含 §POS§ 标记'},
            'payloads': {'type': 'array', 'description': '每个标记一项：{position,type:"list",values:[...]} 或 {position,type:"range",start,stop,step}',
                         'items': {'type': 'object'}},
            'concurrency': {'type': 'integer', 'description': '并发（服务端硬顶 5）'},
            'rate_per_sec': {'type': 'number', 'description': '每秒请求上限'},
            'max_requests': {'type': 'integer', 'description': '总请求上限（服务端硬顶）'},
            'proxy': {'type': 'string', 'description': '显式代理（如 127.0.0.1:1801 代理池入口）；缺省直连'},
        },
        'required': ['template', 'payloads'],
    },
)

tool(
    'browser_navigate',
    group='浏览器',
    flags=("plan_pre",),
    description='内置浏览器导航（F6），允许访问任意目标。成功返回最终 url/标题/状态码；页面流量已自动入抓包历史（人类可在浏览器页查看/重发）。每个动作落审计。',
    input_schema={
        'type': 'object',
        'properties': {
            'url': {
                'type': 'string',
                'description': '目标 url 或裸 host',
            },
        },
        'required': [
            'url',
        ],
    },
)

tool(
    'browser_click',
    group='浏览器',
    flags=(),
    description='内置浏览器点击当前页元素（CSS 选择器或页面坐标 x/y）。作用于当前会话页面，不重复白名单校验。',
    input_schema={
        'type': 'object',
        'properties': {
            'selector': {
                'type': 'string',
                'description': 'CSS 选择器；与 x/y 二选一',
            },
            'x': {
                'type': 'number',
                'description': '页面横坐标（与 y 搭配）',
            },
            'y': {
                'type': 'number',
                'description': '页面纵坐标',
            },
        },
    },
)

tool(
    'browser_type',
    group='浏览器',
    flags=(),
    description='内置浏览器向当前页输入框填文本（CSS 选择器定位，整值替换）。凭据纪律：登录凭据只在会话内存使用，禁止把凭据写入黑板/任务/发现。',
    input_schema={
        'type': 'object',
        'properties': {
            'selector': {
                'type': 'string',
                'description': 'CSS 选择器',
            },
            'text': {
                'type': 'string',
                'description': '要填入的文本',
            },
        },
        'required': [
            'selector',
            'text',
        ],
    },
)

tool(
    'browser_screenshot',
    group='浏览器',
    flags=("plan_pre",),
    description='内置浏览器对当前页单帧截图（PNG）。截图自动落 artifacts（browser-shots/，挂当前任务归属）并回填路径，人类可在浏览器页查看。',
    input_schema={
        'type': 'object',
        'properties': {},
    },
)

tool(
    'browser_content',
    group='浏览器',
    flags=("plan_pre",),
    description='内置浏览器读取当前页 DOM 文本（inner_text，截 8KB），用于 JS 渲染后的动态页面内容提取（curl 拿不到的部分）。',
    input_schema={
        'type': 'object',
        'properties': {},
    },
)

tool(
    'browser_back',
    group='浏览器',
    flags=(),
    description='内置浏览器后退（历史上一页）。',
    input_schema={
        'type': 'object',
        'properties': {},
    },
)

tool(
    'route_lookup',
    group='知识',
    flags=("plan_pre", "spill_skip", "knowledge"),
    description='[legacy] 按关键词查询旧测试点路由索引。返回测试点 → packs/kb 模块路径；仅用于兼容未迁移资料，当前 Skill 优先使用 skill_open 打开自身目录。',
    input_schema={
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '测试点关键词（中英文皆可，如「文件上传」)',
            },
            'limit': {
                'type': 'number',
                'description': '返回条数上限，缺省 8',
            },
        },
        'required': [
            'query',
        ],
    },
)

tool(
    'skill_open',
    group='知识',
    flags=("plan_pre", "spill_skip", "knowledge"),
    description=(
        '打开当前可用技能的 SKILL.md 或同一技能目录内的资源。cc 风格技能把完整方法写在 SKILL.md，references/scripts/ex'
        'amples/assets 中的文件按需读取；省略 path 时返回正文和资源清单。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                'description': '技能名（如 sqli-test）',
            },
            'path': {
                'type': 'string',
                'description': '可选资源路径，如 references/method.md 或 scripts/check.py',
            },
        },
        'required': [
            'name',
        ],
    },
)

tool(
    'request_authorization',
    group='协作',
    flags=("intent_pre", "collab"),
    description=(
        '申请行为边界授权（M5 D2，orchestrator-efficiency）：发现受当前授权边界限制打不下去时，向人类显式申请——不默默死路记账了事。'
        '三类：scope_expand=扩大授权目标（新目标打之前先申请；批准后自行 bb_add_asset 登记）；impact_escalate=影响证明'
        '升级（如从探测升级到拿权限证明）；rating_override=突破收录口径（发现真实影响但按评级规则到不了 vuln/high，申请按更高口径登记）'
        '。**恒人类决策**（L2 也不自动批），结果投递回你的收件箱（authorization_result/approval_rejected）。等待期间'
        '可继续其他无依赖工作。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'kind': {
                'type': 'string',
                'enum': [
                    'scope_expand',
                    'impact_escalate',
                    'rating_override',
                ],
            },
            'scope_request': {
                'type': 'string',
                'description': '申请内容：新目标清单/申请升级到的影响证明等级/申请突破的口径与目标评级',
            },
            'justification': {
                'type': 'string',
                'description': '为什么必须扩（审批人主要看这个）',
            },
            'evidence_finding_ids': {
                'type': 'array',
                'items': {
                    'type': 'string',
                },
                'description': '支撑证据的 finding id 列表',
            },
        },
        'required': [
            'kind',
            'scope_request',
            'justification',
        ],
    },
)

tool(
    'declare_intent',
    group='计划',
    flags=("plan", "intent_flow"),
    description=(
        '声明意图（思考/规划产物）：把一句【可证伪假设】登记到链路图，如「验证 /admin 是否存在未授权访问」。随后围绕它执行（http/工具动作按时间归入'
        '该意图，执行层是图的展开细节）。【必须有资产锚点】填 target_asset_id，或 basis_refs 至少含一条 asset:<资产id>——'
        '否则拒绝：游离意图落不到链路图子目标下，其 dead_end 收尾也无法为资产背书 tested_clean。每个意图最终必须 close_intent'
        ' 收尾为漏洞/发现/死路，不得悬挂。**会话第一次实质动作（run_cmd/浏览器点击/写黑板）前必须先 declare_intent**（服务端意图先'
        '行闸：无 open 意图时实质动作被拒；只读侦察不受限）；**边干边写**——围绕假设执行时每确认一条认知立即 bb_add_finding(categ'
        'ory=intel, status=unverified) 落账，收尾时再升 verified 或随死路转 dead_end，别攒到最后补记。**负结论'
        '（未发现/测过没事）走死路收尾**：close_intent outcome=dead_end + dead_reason + evidence_ref'
        's——这是资产的 tested_clean 背书来源，别把「未发现」当成没产出而不收尾。basis_refs 写推导依据（从什么资产/发现逻辑推出本假设'
        '，形如 asset:asset-6971f089d5fe / finding:find-c32f449cc7b5——<id> 是完整 id，自带 ass'
        'et-/find- 前缀），引用必须已在本项目，悬空即拒。同陈述的未关闭意图已存在 → 直接返回既有意图（merged=true）。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'statement': {
                'type': 'string',
                'description': '一句可证伪假设（≤500 字，勿写动作清单）',
            },
            'target_asset_id': {
                'type': 'string',
                'description': '意图针对的资产 id（host/domain/子目标）。与 basis_refs 的 asset: 锚点二者至少其一',
            },
            'dimension': {
                'type': 'string',
                'description': (
                    '本意图所属的【测试面】id（本轨面清单见系统提示/维度清单，'
                    '如 unauth/sqli/upload）——用于判定"该资产各面是否都测过"；'
                    '填了必须是对本轨合法的面 id，否则拒绝。'
                ),
            },
            'basis_refs': {
                'type': 'array',
                'description': (
                    '推导依据引用：["asset:asset-6971f089d5fe", "finding:find-c32f449cc7b5", …]——本意图从哪些已'
                    '有事实逻辑推出（边=逻辑推导）；<id> 是完整 id（自带 kind 前缀）'
                ),
                'items': {
                    'type': 'string',
                },
            },
        },
        'required': [
            'statement',
        ],
    },
)

tool(
    'close_intent',
    group='控制',
    flags=("control", "intent_flow"),
    description=(
        '收尾意图（意图必收尾，结果三选一）：① outcome=vuln——确认漏洞：finding_ids 至少一条已登记漏洞（category=vuln、非'
        '误报）；② outcome=finding——有效发现：finding_ids 至少一条有效线索（category=intel、非误报），一个意图可带多'
        '条同类发现（目录爆破 5 条=一次发现收尾）；③ outcome=dead_end——死路：dead_reason 必填（什么证据排除了假设、已试过什么'
        '）+ evidence_refs 至少一条 http:/event:/artifact: 证据引用；死路必须零发现。**未发现/测过没事也是结论**：必'
        '须用它收尾，而不是让意图悬挂——资产的 tested_clean 判定就靠 dead_end 背书。证据不足就保持 open（宁严勿松）；有新证据先 r'
        'eopen_intent。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'intent_id': {
                'type': 'string',
            },
            'outcome': {
                'type': 'string',
                'enum': [
                    'vuln',
                    'finding',
                    'dead_end',
                ],
            },
            'finding_ids': {
                'type': 'array',
                'description': 'outcome=vuln/finding 时必填：收尾引用的发现 id',
                'items': {
                    'type': 'string',
                },
            },
            'evidence_refs': {
                'type': 'array',
                'description': (
                    'outcome=dead_end 时必填：证据引用["http:1234", "event:567", "artifact:art-abc"]——htt'
                    'p/event 为纯数字历史/事件 id；artifact:<id> 的 <id> 是完整 id（注意 id 前缀是 art-）'
                ),
                'items': {
                    'type': 'string',
                },
            },
            'dead_reason': {
                'type': 'string',
                'description': 'outcome=dead_end 时必填：死因（什么证据排除假设）',
            },
        },
        'required': [
            'intent_id',
            'outcome',
        ],
    },
)

tool(
    'reopen_intent',
    group='控制',
    flags=("control", "intent_flow"),
    description='重开已关闭意图：收尾所依据的漏洞被标误报、或出现新证据时调用。清空收尾结论回到 open（证据引用保留），下游不级联推翻（推导边只断开，已衍生的新意图独立存活）。',
    input_schema={
        'type': 'object',
        'properties': {
            'intent_id': {
                'type': 'string',
            },
            'note': {
                'type': 'string',
                'description': '重开原因（新证据是什么）',
            },
        },
        'required': [
            'intent_id',
        ],
    },
)

tool(
    'bb_delete_intent',
    group='黑板',
    flags=("intent_flow",),
    description=(
        '物理删除意图（2026-10-01）：误声明/目标取消时硬删该意图行。**带保护**：已收尾（closed）意图拒删——收尾结论是链路图事实，须先 re'
        'open_intent 重开再删；被其他意图引用为推导依据也拒删。删除留痕事件 intent.deleted。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'intent_id': {
                'type': 'string',
                'description': '要删除的意图 id（bb_query 链路/事件可见）',
            },
            'reason': {
                'type': 'string',
                'description': '删除原因（留痕，可空）',
            },
        },
        'required': [
            'intent_id',
        ],
    },
)


# ---------------- 派生常量（导入名与迁移前逐字一致） ----------------

def _names_with(flag: str) -> set:
    return {s.name for s in REGISTRY.values() if flag in s.flags}


# Anthropic schema 全集（Agent 会话/对话轮/编排器/API 目录共用）
AGENT_TOOLS = [s.as_schema() for s in REGISTRY.values()]

# 收尾/计划原语：任何角色都必须可达（§6.6 角色 tools 白名单不拦它们）
_CONTROL_TOOLS = _names_with("control")
_PLAN_TOOLS = _names_with("plan")

# 计划闸（A2）放行面 = 计划原语 ∪ 只读/规划类
_PLAN_PRE_ALLOWED = _PLAN_TOOLS | _names_with("plan_pre")

# 意图流程工具：不算「意图声明后的执行证据」（意图先行门禁判据）
_INTENT_FLOW_TOOLS = _names_with("intent_flow")

# 意图先行闸放行面 = 计划闸放行面 ∪ 控制原语 ∪ 意图流程 ∪ 协调/审批/资产登记
_INTENT_PRE_ALLOWED = (_PLAN_PRE_ALLOWED | _CONTROL_TOOLS
                       | _INTENT_FLOW_TOOLS | _names_with("intent_pre"))

# H1 spill 豁免（正文本身即取用目的）
_SPILL_SKIP = _names_with("spill_skip")

# 分组用集合（agent_tool_group 消费）
_COLLAB_TOOLS = _names_with("collab")
_KNOWLEDGE_EXTRA = _names_with("knowledge")
_FILE_TOOLS = _names_with("file")


def agent_tool_group(name: str) -> str:
    """工具所属目录分组（GET /api/agent-tools 用）。未知工具回落「其他」。"""
    spec = REGISTRY.get(name)
    return spec.group if spec is not None else "其他"
