# webui/src/views/fuzzer/

> Web Fuzzer 重放工作台（2026-10-07）：左 raw 报文编辑器 / 右完整响应，秒级 POC 迭代。
> 顶级导航「重放」（轨门控 pentest/redteam/ctf，与 F6 内置浏览器同口径）。

## 组件树

- `FuzzerView.tsx` 入口：顶部工具条 + 左 raw 编辑器 + 右响应（react-resizable-panels
  v4 `Group/Panel/Separator`，默认 50/50、各最小 20%）。**`defaultSize` 必须写带 `%` 的字符串**
  —— v4 里 number 是**像素**，写 `defaultSize={50}` 会被 `minSize="20%"` clamp 成 20%（左侧塌窄）。
  两侧 header 统一 `px-2 py-1.5 text-[11px]`、编辑区统一 `min-h-0 flex-1 border-0 p-2` 铺满，保证上下对齐。
  - **工具条**：发送请求（Alt+Enter / Ctrl+Enter）/ 停止 / 构造请求（表单式建包弹层）/
    历史（左抽屉）/ 强制HTTPS / 国密TLS / 跟随重定向 / 跳过证书校验 / 设置代理（+ 地址输入）/
    响应体长度限制（KB）。工具条控件用本地 `Toggle` 小组件（自绘开关）。
  - **国密TLS 开关**：挂载时 `api.gmStatus(pid)` 探测；`available=false` 时**置灰**
    并在其下显黄条给出 `guide` 安装指引——**绝不静默降级成普通 TLS**（那会制造
    「以为走了国密、实际没走」的安全假象）。
  - **全屏**：请求编辑器可全屏（隐藏响应区与抽屉）。
  - 轨门控：`!track || track ∈ {pentest, redteam, ctf}`，否则整页提示不可用。
- `ResponsePane.tsx` 响应区：状态行（`状态码 · 耗时ms · 字节数` + 国密/代理/已截断/二进制徽章）
  + **完整原始响应报文单栏**（`HTTP/1.1 <status>` + 响应头 + 空行 + 响应体，对齐 Burp/Yakit
  原始报文形态；**不再拆「响应体 / 响应头」tab**）；**美化**（JSON parse+2 空格缩进，**仅作用体段**，失败置灰）；
  **字符集**下拉（UTF-8/GBK/GB18030/Big5/Shift_JIS/ISO-8859-1）——后端以 utf-8 解码入库，
  故 `TextEncoder` 可无损还原字节再按所选字符集 `TextDecoder` 重解（乱码救场）；
  **定位**输入框（大小写不敏感，命中即 `setSelectionRange` 滚动到首处）。失败行红条显
  `meta.error`；二进制 body 显占位不渲染正文。
- `HistoryDrawer.tsx` 重放历史左抽屉：本项目 `source=replay` 记录（`browserHistory`
  倒序 cap 100，**仅打开时拉取不轮询**），点选回填编辑器并载入该次响应。

## 关键约定

- **传输双路（后端 `core/browser/replay.py`）**：默认 httpx（显式代理、恒 `trust_env=False`）；
  勾「国密TLS」切 `gmhttp` sidecar（`tools/bin/gmhttp.exe`，Go + tjfoc/gmsm gmtls）。
  Python 生态无带 SM 密码套件的 TLS 栈，故国密必须外挂（详见 `tools/CLAUDE.md`）。
- **停止**：`api.replayStop(pid, runId)` → 后端置 stop Event。httpx=放弃等待（sync 不可真中止），
  国密 sidecar=kill 子进程（真停止）。`runId` 由 `browserReplay` 返回。
- **红线**：重发人类 UI 专属——Agent 无任何发起入口，只能只读 `http_history`。
  目标不设门禁（授权边界由使用者负责，与 F6 浏览器同口径）。
- **入口**：`goto-fuzzer` CustomEvent（`detail.raw` 预填报文）。抓包行「✉ 去重放台…」与
  LiveRoom 顶栏 🧪 均走此事件；App.tsx 用 `key={fuzzerNav?.n}` 重挂以套用新预填。
- 轮询：Job 用 `pollJob`（700ms）；历史抽屉打开才拉，不轮询。
- 样式：Tailwind + `cn()`；状态色用 `--status-ok`/`--status-paused` 等 token，勿写死 hex。
