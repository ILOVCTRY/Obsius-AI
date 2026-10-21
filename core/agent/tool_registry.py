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
# 逆向只读工具的 binary 入参口径（2026-10-13）：样本须**已分析**（有 headless 缓存）。
# 未分析时工具快速失败回 [未分析] 而非静默触发数十分钟的全量分析。
_BINARY_DESC = "已分析样本的二进制路径（未分析会回 [未分析]，本工具不自动触发分析）"


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
        '执行命令。runtime 按威胁等级选择：docker=Linux 渗透工具箱（bash + nmap/sqlmap/dirsearch/ffuf/python3 '
        '全套，workspace 挂载 /workspace；能力清单显示 pentest-box 就绪时渗透/扫描/文本命令首选，未就绪回退 wsl）；'
        'host=Windows 特调（PowerShell，仅 Windows 目标/命令）；wsl=bash 兜底；'
        'sandbox=恶意样本（唯一选择，威胁等级不得虚报 trusted）。落盘纪律见系统提示工具纪律。'
        '输出上限 2000 字符，超出截断并标注——读大文件用 read_file，或用 grep -n 定位后小窗口（≤100 行）分段。'
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
                'description': '必须显式传入。按 threat_class 选择允许的运行时：trusted 可用 host/wsl/docker/sandbox；untrusted 仅 docker/sandbox；malware_live 仅 sandbox。'
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
                'description': (
                    '仅 sandbox/docker 有效。省略时 sandbox 默认 none、其余默认 bridge。'
                    '**docker 省略即 bridge＝已能出网，绝大多数题目不需要传这个参数**'
                    '（实测容器默认 bridge 可直连外网服务与 libc.rip）。'
                    'real 与 bridge 在 docker 下走的是同一个 docker 网络，唯一区别是 real 强制'
                    '走一次性审批链——而平台没有 net_real 的申请入口，只能由人类在审批页手工放行，'
                    '因此**不要为了出网去传 real**。'
                    'none 才是断网（明确要隔离外部网络时才用）。'
                ),
            },
            'approval_id': {
                'type': 'string',
                'description': ('net=real 必填；必须是当前项目中 status=approved 的一次性审批 id。'
                                '拿不到就别传 net=real——docker 下省略 net 已经是 bridge（可出网）。'),
            },
            'timeout': {
                'type': 'number',
                'description': '超时秒数（可选）',
            },
        },
        'required': [
            'cmd',
            'runtime',
            'threat_class',
        ],
    },
)

tool(
    'read_file',
    group='文件',
    flags=("plan_pre", "file"),
    description=(
        '只读工作区文件：host 原生直接读，不经 WSL/PowerShell（无命令执行面、无启动开销、无引号转义问题）。'
        'path 相对 scratch（与 run_cmd cwd 一致）；返回带行号文本，默认读 100 行、单次上限 400 行；'
        'offset=-N 读末尾 N 行。大文件先用 search_files 按内容定位再按行段读，不要盲猜行号。'
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
    'view_image',
    group='文件',
    flags=("plan_pre", "file"),
    description=(
        '【看图】把图片直接送进你的视觉通道——**有图先看这里，不要先 OCR**。'
        '支持 PNG/JPEG/GIF/BMP/WEBP；返回一句话元信息 + 图片本体，你能真正看到像素。'
        'path 相对 scratch（与 run_cmd cwd 一致），也收工作区绝对路径。'
        '适用：题目给的是图/截图/二维码/频谱图/位平面图/拼图碎片/UI 截图，'
        '以及需要判断「这张图里写了什么/长什么样/哪里不对」的一切场合。'
        '**OCR（tesseract/zbarimg）只在下面三种情况更优**：① 要批量扫几十上百张候选'
        '（如位平面暴力枚举）；② 要机器可比的精确字符串（逐字符 diff）；'
        '③ 图糊到人眼也难辨、只能靠算法容错。其余一律先 view_image 看一眼——'
        '看错方向再 OCR 比先 OCR 再猜方向省得多。'
        '图片过大自动缩到 max_edge；当前供应商未声明视觉能力时只回元信息并给 OCR 指引。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': '图片路径，相对 scratch（如 att-abc123/flag.png）；也收工作区内绝对路径',
            },
            'max_edge': {
                'type': 'integer',
                'description': '长边像素上限，缺省 1568；隐写/二维码题看不清细节时可调大（token 随之增加）',
            },
            'note': {
                'type': 'string',
                'description': '可选：你希望重点看什么（如「读出图中的 flag」「这个二维码能否扫出」「两半是否对得上」），会一并带给视觉通道',
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
        '在工作区文本文件中按内容检索：纯 Python 实现、不经 shell（无命令执行面、无注入风险），计划前也允许调'
        '（等价只读侦察）。默认搜 scratch 及其余工作区目录，只搜本项目；命中行以「相对路径:行号:内容」返回。'
        '适合在 bb_query 溢出落盘文件里定位关键词，替代 run_cmd grep。'
        '未指定 path 时不扫其它题目的附件解包副本（非本会话 unpack_attachment 解压过的 scratch/att-*），'
        '防多题共跑时旧题工作文件淹没命中；要跨题找就显式传 path。'
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
    'unpack_attachment',
    group='文件',
    flags=("file",),
    description=(
        '解压题目/样本附件 zip 到**确定性工作目录**，替代 run_cmd 拼 unzip：服务端一次完成'
        '「清旧残留 → 安全解压 → 结构化清单」，同题重开/多题共跑时不会新旧混装。'
        '目标目录固定 `scratch/att-<sha12>/`（sha12=附件文件名前缀，回退内容 sha256 前 12 位），'
        '附件行（📎）会直接给出这个路径。path 收 /workspace/... 绝对形态、工作区内绝对路径，'
        '或只传文件名（自动到 artifacts/attachments/ 找）。'
        '返回文件清单（相对 scratch 的路径+大小+类型），之后 read_file/search_files 直接用 '
        '`att-<sha12>/...`；未指定 path 的默认检索已收窄到本会话解压过的附件目录（别的题的工作副本不会被扫到）。'
        'zip slip（路径穿越/绝对路径/符号链接）、加密包、超大包（条目>2000 或解压后>512MB 或单文件>128MB）会被拒；'
        'clean=false 保留目标目录旧文件做增量解压。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': '附件 zip 路径：/workspace/artifacts/attachments/<file>（附件行给的原样形态）、'
                               '工作区内绝对路径，或只传文件名（自动去 artifacts/attachments/ 找）',
            },
            'clean': {
                'type': 'boolean',
                'description': '解压前清空目标目录旧残留（缺省 true；多卷/分包增量解压才传 false）',
            },
        },
        'required': [
            'path',
        ],
    },
)

tool(
    'bb_add_asset',
    group='黑板',
    flags=("intent_pre",),
    description=(
        '登记资产（host/domain/service/url/binary；type 省略按值自动识别，识别不出拒收回填）。'
        '资产树自动挂载：url/service 含 IP 自动建 host，domain 自动 DNS 解析挂 host。'
        '重报同值=合并 meta 不插重复行（发布前先 bb_query 查重）。'
        'meta.title=一句话简述；meta.owner=平台标签（.edu.cn 系→edusrc 等，命中的平台规则会注入会话）。'
        '扫描/测试状态走 bb_asset_status，不要写 meta.scanned。'
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
    'bb_declare_challenge',
    group='黑板',
    flags=("intent_flow",),
    description=(
        'CTF 轨专用：登记/复用一道题目（challenge-asset）。用户给了题目描述+附件、而黑板还没有对应题目节点时，'
        '用本工具把题立起来——立完自动绑定当前线程，后续本线程的 declare_intent/bb_add_finding 不必再传 '
        'target_asset_id，会自动归到这道题下；附件本身不用登记（走本题目的子资产通道）。'
        'name=题目名（短、可一眼认出，如「树状化-签到题」）；description=一句话描述；'
        'tags=题型数组（web/pwn/re/crypto/misc/forensics）。同一道题重复调用=复用既有节点，不插重复行。'
        '**仅 CTF 轨可用**，其余轨调用会被拒。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                'description': '题目名（简短、可一眼认出；重复调用会复用既有同名题目）',
            },
            'description': {
                'type': 'string',
                'description': '一句话题目描述（落 meta.title，前端作为该题目的副标题展示）',
            },
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '题型标签数组，如 ["web","misc"]（落 meta.tags，可筛选）',
            },
        },
        'required': [
            'name',
        ],
    },
)

tool(
    'ctf_submit_flag',
    group='黑板',
    flags=("intent_flow",),
    description=(
        'CTF 轨专用：**判定并登记**一个 flag。拿到疑似 flag 用本工具——先做三层机械判定，'
        '通过后写入 **flag 台账**（challenge_flags，flag 的唯一真值）并落一条过程留痕，'
        '题目状态由台账派生（拿满 `meta.flag_count` 条即解出），不必再调 bb_add_finding。'
        '① 格式置信：`flag{...}`/`xxxctf{...}` 等可信前缀、内层有熵；题面占位符（`flag{example}`/'
        '`flag{test}`）、诱饵前缀（`fake_flag{}`）、模板语法（`{% if %}`/`${x}`）会被拒并说明原因。'
        '② 变换回环：flag 从编码/变换解出时传 `source`=原始观测字节 + `transform`=base64/hex/url/rot13，'
        '工具会验证「按该变换编码回去确实出现在 source 里」；回环不成立=解错了，拒收。'
        '③ 跨题防串：与本题已登记或其它题目高度相似（互为子串）的 flag 会被拒（防多题并发串号）。'
        '`candidate=true` 用于形态像但不是正式格式的命中（管理员密码/API key）：只落 intel 候选线索，不判为解出。'
        '**不联网提交**——只做本地判定与登记，向比赛平台提交仍由人完成。**仅 CTF 轨可用**。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'flag': {
                'type': 'string',
                'description': 'flag 原文（完整，含前缀与花括号；不要只传内层）',
            },
            'evidence': {
                'type': 'string',
                'description': (
                    '解法与证据摘要：从哪条路径拿到、关键命令/脚本、为何确信是它'
                    '（落 finding 的 summary，供 writeup 与人工复核）'
                ),
            },
            'source': {
                'type': 'string',
                'description': (
                    'flag 解出来之前的**原始观测字节**（如 base64 密文、hex 串、响应原文片段）'
                    '——与 transform 一起才做回环验证；直接明文拿到就留空'
                ),
            },
            'transform': {
                'type': 'string',
                'description': '从 source 得到 flag 用的变换：base64/b64/hex/url/urlencode/rot13（留空=不做回环验证）',
            },
            'challenge_id': {
                'type': 'string',
                'description': '题目资产 id（留空=自动用本线程归属的题目）',
            },
            'candidate': {
                'type': 'boolean',
                'description': 'true=只登记为候选线索（intel/unverified），不判为解出',
            },
        },
        'required': [
            'flag',
        ],
    },
)

tool(
    'ctf_ammo',
    group='执行',
    flags=(),
    description=(
        'CTF 轨专用：生成**弱口令/默认口令候选字典**。弱口令题的正确做法不是凭记忆打 '
        '`admin/admin`，而是把题面线索机械展开成几十到几百条候选：产品指纹（泛微/致远/'
        'Tomcat/Nacos…）→ 厂商默认口令；用户名/公司名/年份 → 后缀与 leet 变异；'
        '观测到的凭据 → 只取口令部分。'
        '产出两种形态：`pairs=true` 给 `user:pass` 行（直接喂 hydra/ffuf/自定义脚本），'
        '否则给纯口令表。**写进 scratch/ctf-ammo-<tag>.txt 并返回路径**，再用 run_cmd 消费'
        '（例：`hydra -L users.txt -P ctf-ammo-xxx.txt ...`）。'
        '**本工具不执行爆破、不发任何网络请求**——只产候选，用不用、用多少、要不要限速由你决定。'
        '候选表绝不含 flag 形态串（防自己泄题）。**仅 CTF 轨可用**。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'obs_text': {
                'type': 'string',
                'description': (
                    '观测原文（响应头/banner/nmap 输出/登录页提示等）——从中认产品指纹与凭据'
                ),
            },
            'hint_text': {
                'type': 'string',
                'description': (
                    '题面线索原文（题目描述/提示）：产品名、年份、管理员叫什么、公司名等。'
                    '**优先级最高**——线索产出的候选排在观测之前，执行器按序试到上限'
                ),
            },
            'users': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '已知用户名列表（留空=按产品默认账号，再退通用 admin/root）',
            },
            'known_creds': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '已知凭据（`user:pass` 或裸口令），只取口令部分进候选表',
            },
            'pairs': {
                'type': 'boolean',
                'description': 'true=`user:pass` 成对输出（爆破工具直接可用），false=纯口令表',
            },
            'limit': {
                'type': 'integer',
                'description': '候选上限，默认 200（1-500）——再多人工也看不完',
            },
        },
        'required': ['obs_text'],
    },
)

tool(
    'ctf_abandon',
    group='黑板',
    # intent_pre：放弃**必须**不受意图先行闸限制（ctf-abandon，2026-10-20）。
    # 语义死锁：本工具的用途正是「这个方向我打不下去了」——那种时刻模型手上
    # 往往**没有 open 意图**（刚收尾完）或**正挂着一条解不动的意图**。若受闸，
    # 它会先被要求 declare_intent 声明一条新假设才能表达「放弃」，荒谬且更烧
    # 轮次。放行面成员不收任何代价：它只写 meta.abandoned，不执行、不探测。
    flags=("intent_pre",),
    description=(
        'CTF 轨专用：**放弃这道题**（整题级，不是单个意图）或撤销放弃。'
        '放弃后调度层不再为它派新 Worker，但**已登记的 flag 全部保留**'
        '（放弃=不继续投入，不是清空结论）。'
        '**什么该用它**：这题确实解不出来（缺附件/思路穷尽/时间不够）→ `abandoned=true`；'
        '之后若拿到新线索想接着打 → `abandoned=false` 撤销。'
        '**什么不该用它**：单条解题假设不成立 → 用 close_intent(outcome="dead_end")，'
        '那只是这条路不通，题还能继续解；解出来了 → 什么都不用调（ctf_submit_flag 已入台账）。'
        '**CTF 轨没有「资产测干净（tested_clean）」这个概念**——本题终态只看 flag 台账，'
        '别去 bb_asset_status 标 tested_clean。**仅 CTF 轨可用**。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'challenge_id': {
                'type': 'string',
                'description': '题目资产 id（留空=自动用本线程归属的题目）',
            },
            'abandoned': {
                'type': 'boolean',
                'description': 'true=放弃（默认）；false=撤销放弃、恢复继续解题',
            },
            'reason': {
                'type': 'string',
                'description': (
                    '放弃原因（写清卡在哪：缺什么附件/试过哪些方向/为什么判断不可解）'
                    '——会随事件留痕，供人工复盘与 writeup'
                ),
            },
        },
        'required': [],
    },
)

tool(
    'ctf_conclude',
    group='黑板',
    # intent_pre：收尾判断与放弃同理——想收尾时手上**没有 open 意图**是常态
    # （刚把最后一条假设收尾完）。若受闸，模型会被要求先声明一条新假设才能
    # 表达「我做完了」，荒谬且直接催生一条假意图。本工具只写 meta.concluded，
    # 不执行、不探测，放行面成员无代价。
    flags=("intent_pre",),
    description=(
        'CTF 轨专用：**主动收尾**——「这题的答案我已经拿到了，别再派工」，'
        '但**不冒充「已解出」**（题目仍显示 partial/待人工确认）。'
        '收尾后调度层不再为它派新 Worker，已登记的 flag 全部保留。'
        '**什么时候用**：你已经提交了 flag（ctf_submit_flag 回报「候选 N 条待人工确认」），'
        '且你核对过证据、确认答案确实拿到了——此时若继续派 Worker 只是重复劳动。'
        '**什么时候别用**：① 还没提交任何 flag → 先 ctf_submit_flag；'
        '② 只是某条思路不通 → close_intent(dead_end)，题还能继续解；'
        '③ 这题根本解不出来（缺附件/思路穷尽） → ctf_abandon，那才是「放弃」。'
        '**与 ctf_abandon 的区别**：收尾=做完了等确认（状态 partial）；'
        '放弃=不做了可能没解出（状态 abandoned）。两者都不再派工，语义不同。'
        '**人工确认才算解出**：pending 的候选 flag 不计 solved，'
        '只有用户在题目卡片上点确认才变 accepted。收尾只是替你省掉「白派 Worker」，'
        '不替人做「这条 flag 对不对」的判断。'
        '撤销收尾（concluded=false）后调度层可以重新派工。**仅 CTF 轨可用**。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'challenge_id': {
                'type': 'string',
                'description': '题目资产 id（留空=自动用本线程归属的题目）',
            },
            'concluded': {
                'type': 'boolean',
                'description': 'true=收尾停止派工（默认）；false=撤销收尾、恢复可派工',
            },
            'reason': {
                'type': 'string',
                'description': (
                    '收尾理由（写清：已拿到哪条 flag、证据是什么、为什么判断答案完整）'
                    '——会随事件留痕，供人工复核时判断该不该点确认'
                ),
            },
        },
        'required': [],
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
    'bb_delete_assets',
    group='黑板',
    flags=("intent_flow",),
    description=(
        '批量删除非 binary 叶子资产（语义同 bb_delete_asset，一次一批，单次上限 200）。'
        '资产清洗要去掉成百上千条（如泛解析域名族）时用本工具，不要 bb_query 分页 + 逐条 bb_delete_asset'
        '——逐条删会把调用烧在枚举上。有子资产、被发现引用或 binary 样本的条目逐条拒绝并在 failures 回明原因，'
        '其余照删；返回 deleted/failed 计数与明细（各截 50 条）。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'asset_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '要删除的资产 id 数组（单次 ≤200；先用 bb_query what=assets 取 id，再一次性传入）',
            },
        },
        'required': [
            'asset_ids',
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
        '；若源/目标各有一条同 dedup_key 的发现，按 add_finding 并集语义自动折并（证据并集、等级就高）而非拒绝；'
        '跨项目、树结构不安全或子资产重复时会拒绝。'
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
        '流转资产扫描/测试状态（open→visited→scanning→tested_clean/budget_stop/na）。'
        '访问≠测试：访问过标 visited、开始扫描标 scanning、适用面全覆盖标 tested_clean'
        '（有洞与否不改变状态——测出洞直接 bb_add_finding，状态仍可标 tested_clean）；'
        '预算用尽标 budget_stop、确认不适用（非目标协议/离线主机）标 na。'
        'tested_clean/budget_stop/na 必须带 note（服务端强制），让「哪里没挖完、为什么」可对账。'
        'tested_clean 另须四问（tested_what/viewpoint/why_no_finding 见参数说明）+ 逐资产背书：'
        '须有直接围绕该资产的 closed/dead_end 意图（target 或 basis_refs 含本资产 id）——'
        '每资产独立立意，禁止父节点一条意图批量覆盖子树。四问并入审计事件可事后对账。'
        '测出问题直接 bb_add_finding，不要自报状态。'
        '**CTF 轨没有 tested_clean**：本题终态由 flag 台账判定，标它必被拒——'
        '解出后 close_intent(finding) 收尾即可，整题放弃用 ctf_abandon。'
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
        '登记发现。无证据 status=unverified。'
        '复现步骤写 evidence.repro_steps（结构见该字段说明）：渗透/红队轨 verified 门禁=至少一步 code'
        '（或 artifact_id）且该步 expected 非空。'
        'impact（危害事实：拿到什么/影响面）与 remediation（可落地修复）直接落字段，报告按三件套渲染。'
        '**边干边写**：执行中每确认一条认知（端口/版本/未授权状态/接口行为/凭据线索等）立即以 '
        'category=intel + status=unverified 落一条，抗中断/抗压缩/跨意图复用；收尾时再升 verified '
        '或随死路一并了结，别攒到最后补记。注入评级口径（rule:rating:*）时 severity 须按口径判级并填 rating_basis。'
        'CTF 轨：severity=线索级别（critical=关键突破/high=可行动线索/其余=背景备查），vuln_class=线索类别，'
        'false-positive=死路（evidence 写清原因与已尝试清单）。'
        '**一题一条**：同一题目上再登记会自动并入既有条目，补证据/改结论用 bb_update_finding。'
        'category 缺省自动判定（提示类或 info→intel，其余→vuln）。解题脚本/writeup 用 bb_add_artifact 落产物。'
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
                    '证据对象。复现步骤 repro_steps=[{desc, type: http|python|cmd|image, code, expected, '
                    'artifact_id?, stability?, target?}]——desc/expected 必填，code 填 HTTP 原始报文/'
                    'python 脚本/bash 命令（type=image 时改填 artifact_id 指向图片、无 code），多步链按数组顺序；'
                    '这是 verified 门禁认可的结构（旧 poc/pocs/poc_artifact_id 已 legacy，勿再新写）。'
                    '**注意边界**：验证结论/预期vs实际 属于**顶层** verification_result，不要塞进本对象——'
                    '本对象只放证据本体（repro_steps/pocs/relates_to 等），塞进来的键不会渲染进报告，'
                    '也不会被门禁当成「验证结果」'
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
                'description': '危害描述（报告三件套）：影响事实——**以已实证的为准**，实际拿到什么数据/权限、影响面多大；未实证的影响标注推测或留空，不得当既成事实',
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
                'description': '攻击行为、影响范围及机密性/完整性/可用性后果——只写已证实路径上的后果，未实证的链条不得当既成事实',
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
                'description': (
                    '去重指纹：同 target+vuln_class 下同 key 重报自动并入既有条目（证据并集、severity 就高、'
                    'status 只升），不新增行。**缺省=按 vuln_class 自动归一**（推荐）；同一漏洞切勿自造新键，'
                    '否则白白裂出一条。仅当同一目标上确需把同类问题拆成多条独立条目时才显式传不同键。'
                    '改/复现既有条目：先 bb_query what=findings 拿它的 dedup_key 再按原 key 重报。'
                    '**CTF 轨例外（一题一条）**：同一题目上无论传什么 key 都会并入既有条目'
                ),
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
        '修订已有发现（只传要改的字段）。① 降级时 rating_basis 必须同给「规则名+条款+一句话依据」，否则宁可不改；'
        '② 口径外内容（纯暴露面/过期组件等）→ category=intel + severity=low 转有效线索，不要删除；'
        '③ 误报/死路 → status=false-positive（触发撤回传播通知引用方）；'
        '④ evidence 浅层合并：键级覆盖，列表键（repro_steps）整键替换，须传全量。'
        '渗透/红队轨不收 vuln 类 severity=info（服务端拒收；intel 类可到 info；CTF 轨 info 语义不变）。'
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
        '物理删除发现（按 finding_id，reason 必填——删错不可恢复）。仅限垃圾/走查数据（重复噪声、无价值记录）；'
        '**verified 发现不可删除**；误报走 bb_update_finding 改 status=false-positive；'
        '口径外内容（暴露面等）走 bb_update_finding 转 intel 线索而非删除。'
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
        "落产物文件（POC 脚本/抓包/输出）：写项目产物目录 + sha256 落库。"
        "kind='poc' 仅限 .py——报文打不稳的漏洞才用脚本复现；kind='project' 限 R4 组装集成最终交付"
        "（整个重建程序的 zip 包）。CTF：解题/复现脚本与 writeup 必须落 artifact，不写裸文件"
        "（队友与续跑会话靠黑板找现场）。"
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
        '查黑板：findings / assets / events / func（函数知识库）/ blueprint（开发蓝图）/ '
        'site（单站全貌）/ package（分析包）/ feature_tree（功能树）/ logic_block（业务逻辑块）。'
        '**站点现状一律用 what=site 并传 asset**（一次返回根子树全貌）；assets 只是清单面'
        '（按 type/status/tag 筛），其 asset 参数仅做子树过滤、不带 findings/intents——'
        '不要用它当站点查询，也不要按 type 分片拉全量再本地过滤。'
        'findings 带 target_asset_id 精确过滤，返回含 dedup_key：给既有漏洞补证据/升链时用原 '
        'dedup_key + target_asset_id 重报即自动并入，不要自造新键（自造键会裂出新行）。'
        'events 缺省=本项目全部 kind（不止 chat.*），要窄看传 kinds/session_id；limit 小值会截断。'
        '**负结论（未发现/死路）不要塞进 events**：走 declare_intent → close_intent(dead_end)；'
        '情报类非漏洞结论用 bb_add_finding category=intel（severity>=low）。'
        '**逆向硬规则：反编译任何函数前先查 func 防重复劳动。**'
        '样本：what=assets type=binary——行的 value 即 sha256（func 查询的 binary_sha256 / bb_upsert_func 都用它），'
        'meta.filename 是原文件名；不知道 sha 先列样本，不要猜。'
        '分析包：what=package——条目/依赖边/内容树根 tree_ref（read_file <tree_ref>/<path> 读同包文件，'
        '如 exe 依赖的 DLL）；样本资产行 meta.package_id 是来源包 id。'
        '功能树/业务逻辑（先查后写）：what=feature_tree（tree_id 看单棵）/ what=logic_block'
        '（block_id 看单个）——增改前先查现有节点/块，同职责节点用 bb_feature_node update 补 '
        'desc/func_addresses、块用 bb_logic_block_update 增补，不要重复 add；块名同项目同样本唯一，重名会被拒收。'
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
                    'package',
                    'feature_tree',
                    'logic_block',
                ],
            },
            'asset': {
                'type': 'string',
                'description': (
                    '按目标筛资产（id 或精确 value，host/domain）。what=site：必填，返回子树资产+发现+意图（站点全景）；'
                    'what=assets：可选，只回该资产子树内的资产清单（不带发现/意图）'
                ),
            },
            'cross_challenge': {
                'type': 'boolean',
                'description': (
                    'CTF 轨专用：默认 false=只回**本题**（本线程题目子树 + 未归属任何题目的条目），'
                    '其它题目的发现/附件/事件全部不回传（防多题共跑上下文污染）。'
                    '确实要跨题对照（如同一考点多题比较）才传 true。其它轨此参数无效果。'
                ),
            },
            'blueprint_id': {
                'type': 'string',
                'description': 'blueprint 查询：单份蓝图（缺省列全部）',
            },
            'package_id': {
                'type': 'string',
                'description': 'package 查询：指定分析包 id（缺省=当前分析包）',
            },
            'tree_id': {
                'type': 'string',
                'description': 'feature_tree 查询：单棵树（缺省列全部；可按 binary_sha256 筛）',
            },
            'block_id': {
                'type': 'string',
                'description': 'logic_block 查询：单个业务块（缺省列全部；可按 binary_sha256 筛）',
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
            'offset': {
                'type': 'integer',
                'description': (
                    'assets 分页起点（≥0，默认 0），按 created_at 固定排序：下一页 offset += 已回条数，'
                    'counts.has_more=false 即到底。批量枚举资产（如清洗泛解析域名族）时逐页取 id 再交 bb_delete_assets'
                ),
            },
        },
        'required': [
            'what',
        ],
    },
)

# ---------- bb_query what 面按轨裁剪（track-query-split，2026-10-13） ----------
# 渗透与逆向是两个工种：互相用不到的 what 面就不该出现在下发的 schema 里——schema
# 即上下文预算，也是模型注意力分配（给渗透会话塞 feature_tree，只会稀释它真正要
# 用的 site/findings）。家族划分按 taxonomy.TRACKS 四轨的业务形态：
#   research     = 逆向家族（样本/函数/蓝图/分析包/功能树/业务逻辑块）
#   ctf          = 逆向家族**再裁掉分析包**（2026-10-17）：CTF 题面文件走对话
#                  附件 + bb_add_asset（challenge-asset 流），没有「样本分析」
#                  上传工作流——给 CTF 会话下发 package 面，只会换回一句指回
#                  样本分析的错误提示（用户实测截图），平白暴露一个不存在的流程。
#                  func/blueprint/feature_tree/logic_block 保留：pwn/reverse
#                  子专家照常用。
#   pentest/redteam= 渗透家族（单站全貌 site）
# 轨未知/空串（测试、未接线调用方）= 全量回退，行为与裁剪前一致。canonical 注册项
# 保持全量（向后兼容 + bb_query_what_for 的错误提示同源），裁剪只发生在
# `bb_query_schema_for(track)`——由 AgentSession 下发 LLM 前套用（loop.py）。
_BB_QUERY_WHAT_REVERSE = ("findings", "assets", "events", "func",
                          "blueprint", "package", "feature_tree", "logic_block")
_BB_QUERY_WHAT_CTF = ("findings", "assets", "events", "func", "blueprint",
                      "feature_tree", "logic_block")
_BB_QUERY_WHAT_PENTEST = ("findings", "assets", "events", "site")
_BB_QUERY_WHAT_FULL = ("findings", "assets", "events", "func", "blueprint",
                       "site", "package", "feature_tree", "logic_block")
_BB_QUERY_TRACK_WHAT = {
    "research": _BB_QUERY_WHAT_REVERSE,
    "ctf": _BB_QUERY_WHAT_CTF,
    "pentest": _BB_QUERY_WHAT_PENTEST,
    "redteam": _BB_QUERY_WHAT_PENTEST,
}
# 逆向家族专属参数：渗透面整组删掉（模型看不见就不会传，也免得知其存在硬凑）
_BB_QUERY_REVERSE_ONLY_PROPS = ("blueprint_id", "package_id", "binary_sha256",
                                "address", "risk_tag", "tree_id", "block_id")
# CTF 删除的参数：package_id（分析包查询专属，CTF 无此面）
_BB_QUERY_CTF_ONLY_PROPS = ("package_id",)
# CTF 独占参数：cross_challenge（跨题隔离开关，challenge-isolation 2026-10-19）。
# 只对 CTF 轨有意义——其它轨没有「题目」概念，下发只会让模型困惑于一个
# 恒无效的开关（渗透/逆向拿不到它，等于多一个无关旋钮）。
_BB_QUERY_CTF_EXCLUSIVE_PROPS = ("cross_challenge",)

# 三轨共用的使用要点（findings/events/负结论口径一致，避免三份文案各自漂移）
_BB_QUERY_DESC_COMMON = (
    'findings 带 target_asset_id 精确过滤，返回含 dedup_key：给既有漏洞补证据/升链时用原 '
    'dedup_key + target_asset_id 重报即自动并入，不要自造新键。'
    'events 缺省=本项目全部 kind（不止 chat.*），要窄看传 kinds/session_id；limit 小值会截断。'
    '**负结论不要塞进 events**：走 declare_intent → close_intent(dead_end)；'
    '情报类非漏洞结论用 bb_add_finding category=intel。'
)
# 逆向家族（research/ctf）共用：样本 sha、分析包、功能树/业务块三件套的使用纪律
_BB_QUERY_DESC_REVERSE_COMMON = (
    '**逆向硬规则：反编译任何函数前先查 func 防重复劳动。**'
    '样本：what=assets type=binary——行的 value 即 sha256（func 查询的 binary_sha256 / bb_upsert_func 都用它），'
    'meta.filename 是原文件名；不知道 sha 先列样本，不要猜。'
    '功能树/业务逻辑（先查后写）：what=feature_tree（tree_id 看单棵）/ what=logic_block'
    '（block_id 看单个）——增改前先查现有节点/块，同职责节点用 bb_feature_node update 补 '
    'desc/func_addresses、块用 bb_logic_block_update 增补，不要重复 add；块名同项目同样本唯一，重名会被拒收。'
)

_BB_QUERY_DESC_REVERSE = (
    '查黑板（逆向 family：research/ctf）：findings / assets / events / func（函数知识库）/ '
    'blueprint（开发蓝图）/ package（分析包全貌）/ feature_tree（功能树）/ logic_block（业务逻辑块）。'
    + _BB_QUERY_DESC_COMMON
    + _BB_QUERY_DESC_REVERSE_COMMON
    + '分析包：what=package——条目/依赖边/内容树根 tree_ref（read_file <tree_ref>/<path> 读同包文件，'
      '如 exe 依赖的 DLL）；样本资产行 meta.package_id 是来源包 id。'
)

_BB_QUERY_DESC_CTF = (
    '查黑板（逆向 family：ctf）：findings / assets / events / func（函数知识库）/ '
    'blueprint（开发蓝图）/ feature_tree（功能树）/ logic_block（业务逻辑块）。'
    '**默认只回本题**（本线程题目子树 + 未归属任何题目的条目）：别的题目的发现、'
    '附件子资产、执行事件一律不回传——多题共跑时那些条目对本题是纯噪声，会把'
    '你带偏到别人的打法上。assets 里仍能看到别的题的**题目行**（知道项目里还有'
    '哪些题），但要跨题对照才传 cross_challenge=true。'
    + _BB_QUERY_DESC_COMMON
    + _BB_QUERY_DESC_REVERSE_COMMON
)

_BB_QUERY_DESC_PENTEST = (
    '查黑板（渗透 family：pentest/redteam）：findings / assets / events / site（单站全貌）。'
    '**站点现状一律用 what=site 并传 asset**（一次返回根子树全貌）；assets 只是清单面'
    '（按 type/status/tag 筛），其 asset 参数仅做子树过滤、不带 findings/intents——'
    '不要用它当站点查询，也不要按 type 分片拉全量再本地过滤。'
    + _BB_QUERY_DESC_COMMON
)


def bb_query_what_for(track: str | None) -> list[str]:
    """本轨可用的 bb_query what 面（未知/空轨=全量）。"""
    return list(_BB_QUERY_TRACK_WHAT.get((track or "").strip(), _BB_QUERY_WHAT_FULL))


# ARTEX 主控专属面（artex-controller-slim，2026-10-20）：只留它真正会查的三面。
#
# 实测该主控线程 95 次 bb_query 的分布：what=events 93、assets 1、findings 1；
# 参数只用到 what/limit/kinds/session_id 四个。而按 CTF 轨下发的 schema 仍带
# func/blueprint/feature_tree/logic_block（逆向专属）与 20 个参数——那 2325 字符
# 里大半是主控永远碰不到的东西，纯占上下文。
#
# 主控的职责是「看执行事件、看已有资产与发现」，函数知识库与功能树属于 Worker。
_BB_QUERY_WHAT_ARTEX = ("events", "assets", "findings")
_BB_QUERY_ARTEX_KEEP_PROPS = ("what", "kinds", "session_id", "limit", "type", "status")

_BB_QUERY_DESC_ARTEX = (
    '查黑板（ARTEX 主控面）：events（执行事件流）/ assets（已有资产）/ findings（已有发现）。'
    '**主控不查函数库、蓝图、功能树——那些是 Worker 的面**。'
    '看 Worker 干了什么用 what=events（可按 kinds/session_id 窄看，limit 小值会截断）；'
    '盘点已有资产与发现用 what=assets / findings，避免重复派发。'
    '**负结论不要塞进 events**：那是 Worker 的 declare_intent → close_intent(dead_end)，'
    '主控只看不写。'
)


def bb_query_schema_for_artex() -> dict:
    """ARTEX 主控的 bb_query 精简 schema（见 _BB_QUERY_WHAT_ARTEX 注释）。

    与 ``bb_query_schema_for(track)`` 同构但收得更紧：what 只三面、参数只六个。
    **返回全新 dict**，不与注册表共享可变子对象。
    """
    spec = REGISTRY["bb_query"].as_schema()
    props = {k: dict(v)
             for k, v in spec["input_schema"]["properties"].items()
             if k in _BB_QUERY_ARTEX_KEEP_PROPS and k != "what"}
    props["what"] = {"type": "string", "enum": list(_BB_QUERY_WHAT_ARTEX)}
    return {"name": "bb_query", "description": _BB_QUERY_DESC_ARTEX,
            "input_schema": {"type": "object", "properties": props,
                             "required": ["what"]}}


def bb_query_schema_for(track: str | None) -> dict:
    """bb_query 的 Anthropic schema 按轨变体：what.enum、description、参数面三者同步裁到
    本家族（渗透拿不到 tree_id，逆向的 description 不提 site）。**返回全新 dict**
    （可变子对象不与注册表共享，调用方零顾忌）；未知/空轨=canonical 全量 schema。"""
    spec = REGISTRY["bb_query"].as_schema()
    family = _BB_QUERY_TRACK_WHAT.get((track or "").strip())
    if family is None:
        return spec
    props = {k: dict(v)
             for k, v in spec["input_schema"]["properties"].items()
             if k != "what"}
    if family is _BB_QUERY_WHAT_PENTEST:
        for k in _BB_QUERY_REVERSE_ONLY_PROPS + _BB_QUERY_CTF_EXCLUSIVE_PROPS:
            props.pop(k, None)
        desc = _BB_QUERY_DESC_PENTEST
    elif family is _BB_QUERY_WHAT_CTF:
        # CTF：只摘 package_id 参数（分析包面已从 enum 裁掉）；cross_challenge 保留
        for k in _BB_QUERY_CTF_ONLY_PROPS:
            props.pop(k, None)
        desc = _BB_QUERY_DESC_CTF
    else:
        # research（逆向）：cross_challenge 是 CTF 独占开关，此处不暴露
        for k in _BB_QUERY_CTF_EXCLUSIVE_PROPS:
            props.pop(k, None)
        desc = _BB_QUERY_DESC_REVERSE
    props["what"] = {"type": "string", "enum": list(family)}
    return {"name": "bb_query", "description": desc,
            "input_schema": {"type": "object", "properties": props,
                             "required": ["what"]}}


tool(
    'kb_open',
    group='知识',
    flags=("plan_pre", "spill_skip", "knowledge"),
    description=(
        '打开全局知识库模块（packs/kb/ 区，含测试包手册与快照）：直接返回文件正文，不返回路径。'
        'module 走全局形态 `<域>/<快照>/<路径>`（如 web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）.md），也认 Obsidian wikilink '
        '（[[web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）|手册]]）与反引号/引号包裹。'
        '.md/.py/.txt/.json 均按 UTF-8 文本返回（弹药脚本只是文本，执行仍走 run_cmd）。'
        '路径不存在会返回可用模块清单（照清单改选，禁止猜文件名、禁止 .. 穿越）；禁止通读知识库目录。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'module': {
                'type': 'string',
                'description': '知识库模块路径，全局形态 `<域>/<快照>/<路径>`，如 web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）.md（web 漏洞类目）、'
                               'binary/赛事专题/题目知识/ctf-pwn/heap-fsop.md（题目知识原件）；'
                               '也认 Obsidian wikilink [[web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）|手册]]',
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
        '按关键词全文检索全局知识库（大小写不敏感子串）：返回 '
        '{path（即 kb_open 的 module 参数）, source, matches, snippet}。'
        '多关键词空格分隔为 AND 语义，零结果自动放宽（英文 OR；中文按词元稀有度加权覆盖）。'
        '**直接问中文自然语言即可**（如「如何测试文件上传漏洞」「jwt 算法混淆怎么打」），'
        '不需要先拆成关键词——虚词会被自动剔除，术语按稀有度加权排序。'
        'tag 可选：按 frontmatter 分面标签过滤（phase/vuln_class，如 tag=jwt）。'
        '用法：kb_search 定位 → kb_open 细读；中文专题用中文词、英文快照用英文词，必要时各试一次。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '检索词：关键词或中文自然语言问句均可（空格分隔多词为 AND 语义）',
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
        '提一条知识库/技能变更**提案**：只落 pending，批准权在人类，禁止自行改文件；'
        '**仅当本项目开启「知识库迭代」时可用**，关闭会被拒。每会话最多 3 条，触发情形与证据要求见系统提示工具纪律。'
        'kind=skill：mode=edit（改正文）/ create（新建）/ delete（进回收站可恢复）/ suggest（粒度过粗时提拆分建议，'
        'content 为建议文档全文，批准后落技能目录 拆分建议.md，结构变更由人执行），target={name: 技能名}；'
        'edit/create 的 content 须为 SKILL.md 全文，frontmatter name 必须等于技能名，create 时该技能须不存在。'
        'kind=kb：mode=edit/create/rename/delete，target={cap: 能力包名, path: <域>/<快照>/<文件>'
        '（.md/.py/.txt/.json，中文目录允许）, new_path: 仅 rename}。'
        'kind=case：mode=edit/create，本任务 verified 攻击链/跑通 payload 落对应测试包 成功案例.md 补段或 '
        'payloads/ 补弹药，target 同 kb。'
        'kind=index：mode=edit，增补全局 route_index.yaml 本域条目（kb 路径须带域前缀），'
        'target={cap: 能力包名}，content 为本域条目全文。edit/create 必带 content 全文。'
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
            'feature_tree_id': {
                'type': 'string',
                'description': '引用的功能树 id（分析阶段产物，可空）',
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
        '更新开发蓝图（分区更新，别的会话实时可见）。三种用法：'
        '① 模块深析写回：module_name + spec/notes/func_addresses/module_status'
        '（spec=接口约定：函数签名/数据结构/协议格式；自测不过不得标 module_status=tested）；'
        '② 整表重划分：modules_set 替换全部模块；'
        '③ 汇总正文：content_append 增量追加或 content_md 整体替换（数据流/接口表/算法/协议/状态机）。'
        '蓝图整体 status（draft/reviewed/ready/building/built）不归 Agent 管——reviewed/ready 由人类批准，勿申请。'
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
            'feature_tree_id': {
                'type': 'string',
                'description': '重设引用的功能树 id（可空）',
            },
        },
        'required': [
            'blueprint_id',
        ],
    },
)

tool(
    'bb_feature_tree_create',
    group='黑板',
    flags=(),
    description=(
        '建/取当前样本的功能树（每项目每样本唯一，幂等）。功能树是分析阶段独立产物（不嵌在蓝图里）：'
        '自顶向下拆业务功能为节点（如「存档校验」「金币结算」），每个节点可挂函数地址与描述。'
        '后续蓝图可通过 feature_tree_id 引用该树作为施工图纸；业务逻辑块通过 feature_node_id 下钻某节点。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'binary_sha256': {
                'type': 'string',
                'description': '目标样本 sha256（留空=项目级树）',
            },
            'name': {
                'type': 'string',
                'description': '树名（可空）',
            },
        },
    },
)

tool(
    'bb_feature_node',
    group='黑板',
    flags=(),
    description=(
        '功能树节点增删改（分析阶段产物）。op=add/update/delete：'
        'add 必传 name，parent_id 留空为根；update 必传 node_id，可改 name/desc/parent_id/func_addresses'
        '（Agent 不可改 status——通过验收是人类职责）；delete 必传 node_id，级联删子节点。'
        '防环：update 时新父不能是自身或自身后代。'
    ),
    input_schema={
        'type': 'object',
        'properties': {
            'tree_id': {
                'type': 'string',
            },
            'op': {
                'type': 'string',
                'enum': ['add', 'update', 'delete'],
            },
            'name': {
                'type': 'string',
            },
            'node_id': {
                'type': 'string',
                'description': 'update/delete 必填',
            },
            'parent_id': {
                'type': ['string', 'null'],
                'description': 'add 时留空=根节点；update 时可改父',
            },
            'desc': {
                'type': 'string',
            },
            'func_addresses': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '该节点覆盖的函数 hex 地址',
            },
        },
        'required': ['tree_id', 'op'],
    },
)

tool(
    'bb_logic_block_create',
    group='黑板',
    flags=(),
    description=(
        '建业务逻辑块（逆向理解笔记，与蓝图/攻击链并列的第三种载体）：记录函数协作如何构成业务功能——'
        '函数逻辑分析/业务逻辑分析/逆向破解/游戏业务理解的落点（PWN/漏洞利用走攻击链，重建管线走蓝图，不要混用）。'
        '一个块=一项可讲述的业务功能（如「存档校验」「金币结算」「协议握手」），funcs 挂构成该功能的函数与角色注（一句话职责）。'
        'address 必须已登记 func_kb（先 decompile/bb_upsert_func 再挂）；同项目同样本下块名重名会拒收。'
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
            'feature_node_id': {
                'type': 'string',
                'description': '下钻的功能树节点 id（可空）',
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
            'feature_node_id': {
                'type': 'string',
                'description': '重设下钻的功能树节点 id（可空）',
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
                'description': _BINARY_DESC,
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
                'description': _BINARY_DESC,
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
                'description': _BINARY_DESC,
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
                'description': _BINARY_DESC,
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
                'description': _BINARY_DESC,
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
        '私信其他会话窗（黑板异步协调）：同步情报/移交工作/请求协助。'
        'to_session 定向单窗（优先），to_role 广播该角色全部活跃窗（不含自己），两者必须给一个。'
        '收件方在步边界/认领期/对话轮注入，不实时打断对方工作（异步，无同步对话接力）；不设频率硬限额，滥用在审计可见。'
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
            'body_max_bytes': {'type': 'integer', 'description': '响应正文最大保存字节数；缺省使用浏览器配置（默认 64KB），工具会返回已保存全文'},
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

# 联网搜索（2026-10-19，web-search-tool）：四后端自动降级——① 自建 SearXNG
# （config/web_search.json 配 searxng_url，Docker 一行部署、多引擎聚合、免 Key，
# 推荐；出网隔离在 SearXNG 侧）；② **DuckDuckGo HTML 首选免 Key 后端**（纯 httpx，
# 检索质量最好，需配 proxy）；③ Bing HTML（免 Key，但国内会 302 到 cn.bing.com
# 返回泛首页，故带相关性闸，退化即判失败继续降级）；④ ddgs 末位（缺依赖时不可用）。
# 只返回「标题/URL/摘要」检索结果，深读页面用 browser_navigate / browser_replay。
# plan_pre 口径 = 与 browser_navigate 同级的只读侦察原语，不受意图先行闸。
tool(
    'web_search',
    group='浏览器',
    flags=("plan_pre",),
    description=('联网搜索公网引擎，取回「标题/URL/摘要」列表（单条摘要截 300、'
                 '总量截 8000 字符）。适用：查 CVE/NVD 详情、漏洞利用代码、最新'
                 '安全情报等本地知识库之外的公开信息。四后端自动降级：SearXNG 自建'
                 '实例（配 searxng_url）→ DuckDuckGo（首选免 Key、质量最好）→ Bing'
                 '（带退化闸）→ ddgs。engines/lang 仅 SearXNG 后端生效。仅返回检索'
                 '结果、不自动打开网页；深读单页用 '
                 'browser_navigate。'),
    input_schema={
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '搜索关键词（中英文均可，建议含关键术语/编号，如 "CVE-2026-1234 exploit"）',
            },
            'num': {
                'type': 'integer',
                'description': '返回条数 1-20（默认 10；后端实际不足时以实际为准）',
            },
            'engines': {
                'type': 'string',
                'description': '限定 SearXNG 引擎，逗号分隔（如 google,bing,duckduckgo）；留空用实例默认。仅 SearXNG 后端生效',
            },
            'lang': {
                'type': 'string',
                'description': '结果语言（如 zh-CN、en）；留空不限。仅 SearXNG 后端生效',
            },
        },
        'required': ['query'],
    },
)

tool(
    'route_lookup',
    group='知识',
    flags=("plan_pre", "spill_skip", "knowledge"),
    description='按关键词查询测试点路由索引。返回测试点 → packs/kb 模块路径；技能自身目录用 skill_open 打开，二者互补。',
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
                'description': '技能名（中英文均可，须与技能目录名一致，如 sqli-test、注入测试）',
            },
            'path': {
                'type': 'string',
                'description': '可选资源路径（可含中文），如 references/method.md 或 scripts/check.py',
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
        '申请行为边界授权：受当前授权边界限制打不下去时向人类显式申请，不要默默记死路了事。'
        '三类：scope_expand=扩大授权目标（新目标打之前先申请；批准后自行 bb_add_asset 登记）；'
        'impact_escalate=影响证明升级（如从探测升级到拿权限证明）；'
        'rating_override=突破收录口径（发现真实影响但按评级规则到不了 vuln/high，申请按更高口径登记）。'
        '**恒人类决策**（L2 也不自动批），结果投递回你的收件箱（authorization_result/approval_rejected）。'
        'rating_override 是口径受限时的**显式**出口，**不得作为常规抬级手段**'
        '（定级仍以实证危害为准，证据不足宁低勿高——finding-severity-calibration）。'
        '等待期间可继续其他无依赖工作。'
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
        '声明意图（思考/规划产物）：把一句【可证伪假设】登记到链路图，如「验证 /admin 是否存在未授权访问」；'
        '随后围绕它执行，工具动作按时间归入该意图。'
        '【必须有资产锚点】填 target_asset_id，或 basis_refs 至少含一条 asset:<资产id>，否则拒绝'
        '（游离意图落不到链路图子目标下，其 dead_end 也无法为资产背书 tested_clean）。'
        'basis_refs 写推导依据（asset:asset-6971f089d5fe / finding:find-c32f449cc7b5，'
        '<id> 是带前缀的完整 id），引用须已在本项目，悬空即拒。'
        '**会话第一次实质动作（run_cmd/浏览器点击/写黑板）前必须先声明**（服务端意图先行闸：'
        '无 open 意图时实质动作被拒；只读侦察不受限）。'
        '**负结论走死路收尾**：close_intent outcome=dead_end + dead_reason + evidence_refs——'
        '这是资产的 tested_clean 背书来源，别把「未发现」当成没产出。'
        '每个意图最终必须 close_intent 收尾，不得悬挂。'
        '同陈述的未关闭意图已存在 → 直接返回既有意图（merged=true）。'
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
        '收尾意图（每个意图必须收尾，结果三选一）：① vuln——确认漏洞，finding_ids 至少一条 category=vuln 的非误报；'
        '② finding——有效线索，至少一条 category=intel 的非误报；一个意图可带多条同类发现'
        '（目录爆破 5 条=一次发现收尾）；③ dead_end——死路，需 dead_reason + evidence_refs，且必须零发现。'
        '**未发现/测过没事也是结论**：必须用它收尾而非让意图悬挂——资产的 tested_clean 就靠 dead_end 背书。'
        '**CTF 轨例外**：dead_end 只要求 `dead_reason`（写清卡点即可），不必凑 evidence_refs——'
        'CTF 的终态判据是 flag 台账，没有 tested_clean 这个消费者。'
        'CTF 轨的 vuln/finding **不校验 category 匹配**（flag 不是漏洞；题目级并入可能让留痕'
        'id 实际是 category=vuln，解出时统一用 outcome="finding" 传该 id 即可）。'
        '证据不足保持 open（宁严勿松）；有新证据先 reopen_intent。'
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
        '物理删除意图：误声明/目标取消时硬删该意图行。**带保护**：已收尾（closed）意图拒删'
        '——收尾结论是链路图事实，须先 reopen_intent 重开再删；被其他意图引用为推导依据也拒删。'
        '删除留痕事件 intent.deleted。'
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
