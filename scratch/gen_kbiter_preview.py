"""生成知识库迭代开关的视觉校验页（内联产物 CSS，三主题 × 开/关）。"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
css = (ROOT / "webui/dist/assets/index-AQV2Ksa5.css").read_text(encoding="utf-8")

SPARKLES = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5'
    'l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063'
    'a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>'
    '<path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/></svg>'
)


def toggle(on: bool) -> str:
    cls = "kbiter-toggle" + (" is-on" if on else "")
    return (
        f'<button type="button" role="switch" aria-checked="{str(on).lower()}" class="{cls}">'
        f'{SPARKLES}<span>知识库迭代</span><span class="kbiter-tag">本项目</span>'
        '<span class="kbiter-switch" aria-hidden><i></i></span></button>'
    )


def panel(theme: str, label: str) -> str:
    attr = "" if theme == "cyber-dark" else f' data-theme="{theme}"'
    return f"""<section{attr} class="panel">
  <p class="plabel">{label}</p>
  <div class="row">{toggle(False)} {toggle(True)}</div>
</section>"""


html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}</style>
<style>
  body {{ margin: 0; padding: 22px; background: var(--background); color: var(--foreground);
         font-family: ui-sans-serif, system-ui, "Segoe UI", sans-serif; }}
  .panel {{ margin: 0 0 16px; padding: 16px 18px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-1); }}
  .plabel {{ margin: 0 0 12px; font-size: 11px; color: var(--muted-foreground); font-family: ui-monospace, monospace; }}
  .row {{ display: flex; align-items: center; gap: 18px; }}
</style></head><body>
{panel("cyber-dark", "cyber-dark（默认）")}
{panel("warm-dark", "warm-dark")}
{panel("light", "light")}
</body></html>"""

out = ROOT / "scratch/verify-kbiter.html"
out.write_text(html, encoding="utf-8")
print("wrote", out)
