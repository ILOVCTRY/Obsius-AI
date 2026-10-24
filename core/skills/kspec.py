"""知识库手册写作规范（判据式手册）——**注入用的机器口径**。

单一数据源：模板骨架与硬性规则只在这里定义一次，两处提示共用：

- ``core/chat/runtime.py`` —— 对话链（主控/子专家）用 ``propose_pack_edit`` 提提案时
- ``core/skills/kbiteration.py`` —— 轮末自动提案的 LLM system prompt

**为什么必须注入、不能让模型自己查**（2026-10-21 Nan 报 bug）：
详版规范 ``packs/kb/知识库文章写作规范（判据式手册）.md`` 放在 ``packs/kb/`` 根目录，
而 ``load_kb_sources`` 只登记 ``packs/kb/<域>/``，导致：

1. ``kb_search`` 索引不到（``_iter_files`` 遍历各域 root）——实测搜「提案模板」
   「写作规范」「propose_pack_edit」**全部 0 条**；
2. ``kb_open`` 打不开（``resolve_kb`` 只在域 root 下解析）；
3. ``read_file`` 只能读 scratch 工作区，读不到 packs。

三条路全断 = 模型永远拿不到模板，只能反复 ``kb_search`` 空转（截图里的
5 连空搜）。所以模板必须**进系统提示**，而不是指望模型主动检索。

注入内容刻意精简（约 25 行）：完整规范留在 md 里供人读，提示里只放
「结构骨架 + 会被机械质检拒的三条」，多了会挤占上下文且模型不会照读。
"""

from __future__ import annotations

from pathlib import Path

#: 详版规范的文件名（相对 ``packs/kb/``）。仅用于提示里给出深读入口。
SPEC_FILENAME = "知识库文章写作规范（判据式手册）.md"

#: ``packs/kb/`` **根目录**下允许被 kb_open 直接打开的文档（白名单，防越界读）。
#: 这些文件不属于任何能力域，``resolve_kb`` 按域解析必然失败，需 kb_open 兜底。
#: 只列明确给 Agent 看的元文档——根目录还有 route_index.yaml / synonyms.yaml /
#: licenses/ 等，不应进白名单。
ROOT_DOCS: tuple[str, ...] = (
    SPEC_FILENAME,
    "知识库全局根（expert-pool M0，2026-09-21）.md",
)

#: 注入正文。调用方原样 append 到 system prompt 末尾。
WRITING_SPEC_BLOCK = """\
## 提案写作格式（机械质检会拒，务必照写）

你写的 kb 手册必须是下面两种结构之一。首节不是表、或节内缺「不成立」，提案会被直接拒绝。

**模板 A · 穷举型**（入口唯一但方向多：图片隐写、MISC、编码、协议异常）
```
## 测试面
| # | 方向 | 判据（怎么发现） | 动作 |
|---|---|---|---|
| 1 | 元数据检查 | `exiftool` 有非常规字段 | 读 EXIF/XMP/ICC/comment 全字段 |

## 1. 元数据检查
**前置**：`exiftool` 可用（缺则走 §1.1 纯 Python 路径）
**判据**：可观测到的现象（字段值/报错/结构异常）
**动作**：可复制执行的命令
**不成立**：什么情况说明该方向没有 → 转哪一节
**降级**：工具缺失/无内核接口/权限不足时改走什么（给可执行的替代，不是「换工具」）
```

**模板 B · 选路型**（现象已定、手法互斥：PWN、Crypto、反序列化、堆利用）
```
## 判据速查
| 手上的原语 / 卡在哪 | 首选手法 | 去哪节 |
|---|---|---|
| 有 UAF + 能改 freed 块的 `fd` | tcache poisoning | §1 |

## 1. tcache poisoning
**前置**：glibc 2.26–2.31（2.32+ 有 safe-linking，见 §1.1）
**判据**：… **动作**：… **不成立**：…
**降级**：该手法不成立时改走哪条（或「此路不通，转 §N」）
```

**四条硬规则**（均由 `proposals.py` 机械校验，违反直接 422 拒绝）：
1. 首节必须是表（`## 测试面` 或 `## 判据速查`），表头三列固定语义
2. 每个方向/手法一个 H2，节内必写「判据 → 动作 → 不成立」三段
3. **每个带代码块的手法节必须写「前置」与「降级」**（2026-10-23 新增，见下）。
   容忍度：允许最多 40% 的手法节缺失（纯数学/纯协议节写「前置」只会变成
   填充噪音），但整篇都不写一定被拦
4. 禁写 `本题`/`实测`/`该题`/`这道题` 叙述；答案、帧号、偏移、哈希一律不入正文，
   只留阈值与量级（`< 30%`、`高一个数量级`）

**为什么「前置 + 降级」是硬规则**（R3 的根因归纳：**手册默认工具可用，比工具缺失
更贵**——查不到还能自己摸，查到一条「看起来完全合理」的判据却跑不通，最费时间）：
- **前置** = 这条手法成立必须满足什么（工具装了没、内核接口有没有、版本区间、
  权限要求）。典型：VeraCrypt 手册的入口是 `cryptsetup tcryptDump`，而容器里
  无二进制 → 装了无 dm-crypt 内核接口 → 整条断；`__libc_csu_init` 搬运段的
  寄存器顺序随编译选项变，照抄固定顺序会静默崩溃。
- **降级** = 前置不成立时**改走什么**，要写成可执行的替代路径（如「手写
  PBKDF2 + AES-ECB 拼 XTS，用双 CRC 当口令 oracle」），不能只写「换工具试试」。
- 判据：**读者在只有最朴素环境（无该工具/无权限/无网络）时，能靠这一节走完吗？**
  不能，就说明缺降级路径。

判据自检：**换个同类型但不同题的目标，这段话还有用吗？** 答案为否 = 写成了流水账，删掉重写。

**target 怎么写**：`kb={cap:<域>, path:<域>/<路径>.md}`。
`cap` 就是 `path` 的**第一段**（`web`/`binary`/`crypto`/`forensics`/`misc`/`cloud`），
**不是** `kb_search` 输出里的「源」字段（那是内部标识，形如 `forensics-kb`）。
写错会让提案在落库前被拒（`[拒绝] 提案 target 参数非法`）。

完整规范（含反面样例、单题素材处理、三面同步）可用
`kb_open("知识库文章写作规范（判据式手册）.md")` 读；但上面的骨架已够写，不必先读。
"""


def spec_block() -> str:
    """返回注入用的规范段（带前后空行，便于 append）。"""
    return WRITING_SPEC_BLOCK


def resolve_root_doc(packs_root: str | Path | None, rel: str) -> Path | None:
    """把 ``packs/kb/`` 根目录元文档的相对名解析为真实路径；不命中返回 None。

    只认 ``ROOT_DOCS`` 白名单（裸名或 ``kb/`` 前缀两种写法），且必须落在
    ``packs/kb/`` 之内——防 ``..`` 穿越与变成「任意根目录读」。
    放在模块级而非 ToolDispatcher 方法，是为了让只读桩（测试里常见
    ``SimpleNamespace``）也能复用同一份判据。
    """
    if not packs_root or not rel:
        return None
    base = (Path(packs_root) / "kb").resolve()
    for name in ROOT_DOCS:
        if rel not in (name, f"kb/{name}"):
            continue
        fp = (base / name).resolve()
        if base not in fp.parents or not fp.is_file():
            return None
        return fp
    return None
