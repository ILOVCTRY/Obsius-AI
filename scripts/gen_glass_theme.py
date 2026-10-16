# -*- coding: utf-8 -*-
"""生成「毛玻璃质感模式」+ 背景壁纸规则，插入 webui/src/index.css。

2026-10-15 改版：毛玻璃**不再是独立主题**，而是可叠加在任一套配色
（cyber-dark/warm-dark/light/rose-dark/rose-light）上的质感模式——
html[data-glass="1"] 时把表面/描边 token 统一换成同色相半透明版。

原理：每套主题都定义同一套 `--surface-*-lch` / `--text-lch` 基色三元组，
本块只在这些基色上统一加 alpha / color-mix，因此对所有配色自动生效，
不复制任何主题的具体色值。

- 暗色四套 alpha 0.38–0.72（透出壁纸）；`light`/`rose-light` 走高 alpha
  （0.78–0.92，磨砂白），避免暗壁纸透上来压垮深色文字。
- 描边/高光从 `--text-lch` color-mix 派生：暗配色出受光亮边，亮配色出深色描边。
- viz 画布类 token 保持实色——var 自引用会成环，无法在模式里给任意色加 alpha。
- 壁纸层 `.wallpaper-layer` 所有模式共用（fixed z-index:-1）；不透明模式下
  被表面完全盖住。内置默认壁纸 SVG；用户上传由 JS 覆写 documentElement 内联
  变量（`--wallpaper-image/dim/fit`，内联值优先于这里的默认值）。
- backdrop-filter 只加在 portal 浮层与主要磨砂件（.app-nav/.wb-aside/
  .wb-composer/.wb-panel/.wb-drawer-panel/.wb-rail/.wb-context-rail/
  .wb-rail-handle/.wb-todo）上——**不加在整屏 .app-shell**（会把整幅壁纸糊掉）。
  且**父层已糊过的子层必须关掉自己的 backdrop-filter**：嵌套 backdrop-filter
  只能拿到父层的半透明底色、丢掉下层壁纸（见块内 .nav-rail-slot .wb-aside）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "webui" / "src" / "index.css"

START = "/* glass-mode:start"
END = "/* glass-mode:end */"

BLOCK_TEMPLATE = """__START__ ---------- 毛玻璃质感 + 背景壁纸（2026-10-15）----------
   毛玻璃不是独立主题，而是可叠加在任一套配色上的「质感模式」：
   html[data-glass="1"] 时统一把表面/描边 token 换成同色相半透明版
   （各主题的 --surface-*-lch / --text-lch 基色不变，alpha 在这里控制）。
   改口径直接改 scripts/gen_glass_theme.py 再重放（幂等，起止标记夹住）。 ---------- */
:root {
  --glass-blur: 22px;
  --glass-saturate: 1.35;
  --glass-highlight: color-mix(in oklab, oklch(var(--text-lch)) 7%, transparent);
  --wallpaper-image: url("./assets/glass-wallpaper.svg");
  --wallpaper-dim: 0.3;
}
/* 暗色四套：玻璃 alpha 0.38–0.72，透出壁纸 */
[data-glass="1"] {
  --surface-0: oklch(var(--surface-0-lch) / 0.4);
  --surface-1: oklch(var(--surface-1-lch) / 0.5);
  --surface-2: oklch(var(--surface-2-lch) / 0.58);
  --surface-3: oklch(var(--surface-3-lch) / 0.68);
  --surface-raised: oklch(var(--surface-raised-lch) / 0.62);
  --surface-border: color-mix(in oklab, oklch(var(--text-lch)) 18%, transparent);
  --background: oklch(var(--surface-0-lch) / 0.38);
  --card: oklch(var(--surface-1-lch) / 0.55);
  --popover: oklch(var(--surface-2-lch) / 0.72);
  --secondary: oklch(var(--surface-1-lch) / 0.5);
  --muted: oklch(var(--surface-1-lch) / 0.45);
  --accent: oklch(var(--surface-2-lch) / 0.55);
  --border: color-mix(in oklab, oklch(var(--text-lch)) 16%, transparent);
  --input: color-mix(in oklab, oklch(var(--text-lch)) 22%, transparent);
}
/* 明亮两套：磨砂白走高 alpha，暗壁纸透上来也不压垮深色文字。
   注意 --background 只给到 0.55——它是 .app-shell 的整屏底色（index.css
    components 层 `.app-shell{background:var(--background)}`），给太高会把壁纸
   整片洗白（实测 0.8 时 light 配色下壁纸完全不可见）。壁纸上再叠一层白色
   提亮罩（与暗配色的暗色 dim 罩同族，复用同一 dim 滑块）保 shell 级文字可读。 */
[data-glass="1"][data-theme="light"],
[data-glass="1"][data-theme="rose-light"] {
  --glass-nav-a: .9;
  --glass-aside-a: .9;
  --glass-composer-a: .94;
  --glass-panel-a: .86;
  --surface-0: oklch(var(--surface-0-lch) / 0.78);
  --surface-1: oklch(var(--surface-1-lch) / 0.82);
  --surface-2: oklch(var(--surface-2-lch) / 0.86);
  --surface-3: oklch(var(--surface-3-lch) / 0.92);
  --surface-raised: oklch(var(--surface-raised-lch) / 0.86);
  --background: oklch(var(--surface-0-lch) / 0.55);
  --card: oklch(var(--surface-1-lch) / 0.86);
  --popover: oklch(var(--surface-2-lch) / 0.92);
  --secondary: oklch(var(--surface-1-lch) / 0.8);
  --muted: oklch(var(--surface-1-lch) / 0.78);
  --accent: oklch(var(--surface-2-lch) / 0.84);
}
/* 明亮配色的 dim 罩改成提亮（白）：暗壁纸上的浅色玻璃需要亮底衬托 */
[data-glass="1"][data-theme="light"] .wallpaper-layer::after,
[data-glass="1"][data-theme="rose-light"] .wallpaper-layer::after {
  background: oklch(1 0 0 / var(--wallpaper-dim, 0.3));
}
/* 壁纸层：body 直接子元素，所有模式共用；不透明模式下被表面完全盖住 */
.wallpaper-layer {
  position: fixed;
  inset: 0;
  z-index: -1;
  background-image: var(--wallpaper-image, none);
  background-size: var(--wallpaper-fit, cover);
  background-position: center;
  background-repeat: no-repeat;
  pointer-events: none;
}
.wallpaper-layer::after {
  content: "";
  position: absolute;
  inset: 0;
  background: oklch(0.08 0.014 250 / var(--wallpaper-dim, 0));
}
/* 壁纸留边（contain）时露出的 body 底色：跟随当前配色的实色 surface-0 */
[data-glass="1"] body { background-color: oklch(var(--surface-0-lch)); }
/* 糊化只加在「浮层」与「主要磨砂件」上——**绝不加在 .app-shell**：
   它是整屏元素，backdrop-filter 会把它身后唯一的一层东西（整幅壁纸）
   糊掉，dim=0% 也一样模糊（2026-10-15 Nan 实测反馈的 bug）。
   · portal 浮层（dialog/menu/listbox）糊它们身后的应用内容+壁纸；
   · 常驻磨砂件（.app-nav/.wb-aside/.wb-composer/.wb-panel + 右侧栏一族
     .wb-drawer-panel/.wb-rail/.wb-context-rail/.wb-rail-handle）吃 theme-raw
     兼容 token 的实色底——玻璃模式下换 color-mix 半透明 + 自身糊化，
     壁纸在间隙里保持锐利（alpha 走变量，明亮两套在下面的守卫里抬档）。 */
[data-glass="1"] {
  --glass-nav-a: .72;
  --glass-aside-a: .72;
  --glass-composer-a: .78;
  --glass-panel-a: .55;
}
[data-glass="1"] [role="dialog"],
[data-glass="1"] [role="menu"],
[data-glass="1"] [role="listbox"],
[data-glass="1"] .app-nav,
[data-glass="1"] .nav-sep,
[data-glass="1"] .wb-aside,
[data-glass="1"] .wb-composer,
[data-glass="1"] .wb-panel,
[data-glass="1"] .wb-drawer-panel,
[data-glass="1"] .wb-rail,
[data-glass="1"] .wb-context-rail,
[data-glass="1"] .wb-rail-handle,
[data-glass="1"] .wb-todo {
  /* 前缀必须写在标准属性**之前**：写在后面会被 lightningcss 去重成只留
     -webkit- 一条，而本机 Edge 不认 -webkit-backdrop-filter → 糊化整体失效
     （2026-10-15 实测：前缀版 stddev 95.5 = 无效果，标准版 1.6）。 */
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
  backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
}
/* 停靠进 .app-nav 的 .wb-aside 必须关掉自己的 backdrop-filter（2026-10-16 实测）：
   .app-nav 自己带 backdrop-filter → 它成为 backdrop root；子层再 blur 时拿到的
   「背景」只剩父层那层半透明底色、**丢掉了下层壁纸** → 63% 深色叠在黑上，
   渲染成近黑（实测停靠面板 (32,23,26) vs 导航 (70,64,66)，Nan 报「这一栏是深色透明」）。
   父层已经糊过，子层不必再糊（父层给的那层半透明底仍在，所以观感与导航齐平）。
   **只在停靠态关**：非停靠态（导航收起/窄栏）它不在 .app-nav 里，仍需自己 blur。
   选择器 (0,3,0) 高于上面名单的 (0,2,0)，且本条在其后，双保险。 */
[data-glass="1"] .nav-rail-slot .wb-aside {
  -webkit-backdrop-filter: none;
  backdrop-filter: none;
}
[data-glass="1"] .app-nav {
  background: color-mix(in oklab, var(--theme-raw-308b572e6b) calc(var(--glass-nav-a) * 100%), transparent);
}
[data-glass="1"] .wb-aside {
  background: color-mix(in oklab, var(--theme-raw-8b524e3722) calc(var(--glass-aside-a) * 100%), transparent);
}
[data-glass="1"] .wb-composer {
  background: color-mix(in oklab, var(--theme-raw-a7cd90308d) calc(var(--glass-composer-a) * 100%), transparent);
}
[data-glass="1"] .wb-panel {
  background: color-mix(in oklab, var(--theme-raw-f07f607b7e) calc(var(--glass-panel-a) * 100%), transparent);
  box-shadow: inset 0 1px 0 var(--glass-highlight);
}
/* 右侧栏一族（工具抽屉 / 发现栏 / 上下文栏 / 收起态手柄，2026-10-16）：
   原先吃 `--theme-raw-8b524e3722` **原色**——它自带 55% alpha 但**不在糊化名单里**，
   于是是一块偏实的深色板（实测 (26–62)），与导航/侧栏那套玻璃不是一族（Nan 报
   「这一栏是深色透明」）。现并入 .wb-aside 同档：同 token、同 --glass-aside-a、同 blur。 */
[data-glass="1"] .wb-drawer-panel,
[data-glass="1"] .wb-rail,
[data-glass="1"] .wb-context-rail,
[data-glass="1"] .wb-rail-handle {
  background: color-mix(in oklab, var(--theme-raw-8b524e3722) calc(var(--glass-aside-a) * 100%), transparent);
}
/* 悬浮待办卡（.wb-todo，2026-10-16 起 sticky 钉在时间线顶部）在玻璃模式下换回「磨砂」：
   非玻璃模式它垫了一层不透明 `oklch(--surface-1-lch)`（防滚动内容透出幽灵字），
   那层实底在玻璃界面里会是一块突兀的卡片；这里去掉垫底，改由上面的 backdrop-filter
   糊掉滚上来的内容（0.8 alpha + blur = 磨砂，仍是可读的浅底）。
   选择器 (0,2,0) 高于 index.css 基础规则 `.wb-todo` 的 (0,1,0)，不靠源码顺序取胜
   （玻璃块在 index.css 里位于 .wb-todo 基础规则**之前**）。 */
[data-glass="1"] .wb-todo {
  background: linear-gradient(180deg, var(--theme-raw-deaf08acbf), transparent 70%), var(--theme-raw-ff24ae504f);
}
/* 左导航右缘的「额外亮」竖带（2026-10-15 Nan 反馈）。合成它的有两层：
   ① `.app-nav` 的 1px 边框吃 `--theme-raw-e8b69cc145` 实色——全应用唯一一处
      没并入玻璃描边档的面板边界线（`@layer base` 的 `*{border-color:var(--border)}`
      被这条 unlayered 规则盖掉），玻璃模式下周围表面都半透明了，它还是一条
      硬实色线；
   ② 2px 拖拽分隔条是 `bg-transparent`——透出 `.app-shell` 的 wash（暗配色
      0.38 alpha，比导航表面 0.72 更透），亮壁纸下成了一条「洞」。
   两者叠出一条贯穿全高、比两侧表面都亮的带子。处理：
   · 边框退成透明，改由分隔条右缘出玻璃发丝线 `--border`（与顶栏/侧栏底边、
     直播间 live-aside 的 `border-l` 同族），发丝线落在真正的分界上；
   · 分隔条延续导航表面的半透明档（并在上面的糊化名单里同样吃 backdrop-filter），
     缝隙不再是洞。
   本块不带 layer、会盖掉 Tailwind 的 hover:bg-accent / data-[separator=active]:bg-accent，
   故高亮反馈必须在这里显式重声明。 */
[data-glass="1"] .app-nav { border-right-color: transparent; }
[data-glass="1"] .nav-sep {
  background: var(--theme-raw-308b572e6b);
  background: color-mix(in oklab, var(--theme-raw-308b572e6b) calc(var(--glass-nav-a) * 100%), transparent);
  border-right: 1px solid var(--border);
}
[data-glass="1"] .nav-sep:hover,
[data-glass="1"] .nav-sep[data-separator="active"] { background: var(--accent); }
@media (prefers-reduced-motion: reduce) {
  .wallpaper-layer { transition: none !important; }
}
__END__
"""

BLOCK = BLOCK_TEMPLATE.replace("__START__", START).replace("__END__", END)


def main() -> None:
    css = CSS.read_text(encoding="utf-8")
    # 幂等：先摘掉旧块（同时兼容上代的 glass-theme:start/end 标记）
    css = re.sub(r'\n/\* glass-(?:mode|theme):start.*?/\* glass-(?:mode|theme):end \*/\n', '\n', css, flags=re.S)
    if "glass-theme:start" in css or "glass-mode:start" in css:
        sys.exit("旧 glass 块未摘干净，请人工检查 index.css")
    # 插在最后一个主题块之后（规则须晚于所有 [data-theme] 块才能覆写 token）
    anchor = '[data-theme="rose-light"] {'
    idx = css.index(anchor)
    end = css.index("\n}\n", idx) + len("\n}\n")
    CSS.write_text(css[:end] + BLOCK + css[end:], encoding="utf-8")

    css = CSS.read_text(encoding="utf-8")
    n_block = len(re.findall(r'\[data-glass="1"\]', css))
    n_theme = len(re.findall(r'\[data-theme="glass"\]', css))
    n_none = css.count("None")
    print(f"inserted glass-mode block: [data-glass=\"1\"] x{n_block}, "
          f'[data-theme="glass"] x{n_theme}, None x{n_none}')
    if n_theme or n_none:
        sys.exit("校验失败：残留 glass 主题块或 None 占位")


if __name__ == "__main__":
    main()
