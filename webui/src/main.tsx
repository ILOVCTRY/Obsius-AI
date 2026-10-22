import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyStoredTheme } from '@/lib/theme'
import { initWallpaper } from '@/lib/wallpaper'

applyStoredTheme()
void initWallpaper()

// 未捕获的 promise 拒绝必须可见（2026-10-21）。
//
// 背景：桌面壳（Electron）在渲染进程把 `window.prompt` 换成
// `throw new Error("prompt() is not supported.")`。而 `onClick={() => void decide()}`
// 这类调用点里，`decide` 是 async —— 函数体内的同步抛错变成 **rejected promise**，
// `void` 把它丢掉，`decide` 内部的 try/catch 也接不到（抛错发生在第一个 await
// 之前，此时还没有 promise 可 catch）。结果是：点按钮**无请求、无报错、无 UI
// 反馈**。当时没有任何全局兜底，连 console 都干净，只能靠读 Electron 源码定位。
//
// 这里只做「让它出声」，不改业务行为：
//   * 永远 console.error（排查用，不限流）；
//   * 页面上弹一条**非模态**提示（最多 3 条），因为用户真正感知的是「点了没反应」。
// 不用 confirm/alert：它们会打断正在进行的操作，而这里需要的是「告知」不是「确认」。
const TOAST_MAX = 3
let shown = 0

function notifyFailure(msg: string) {
  if (shown >= TOAST_MAX) return
  shown += 1
  const box = document.createElement('div')
  box.setAttribute('role', 'alert')
  box.style.cssText = [
    'position:fixed', 'right:16px', 'bottom:16px', 'z-index:2147483647',
    'max-width:420px', 'padding:10px 12px', 'border-radius:8px',
    'border:1px solid var(--status-error, #b42318)',
    'background:var(--background, #111419)', 'color:var(--foreground, #e5e7eb)',
    'font:12px/1.5 ui-sans-serif,system-ui,sans-serif',
    'box-shadow:0 8px 24px rgba(0,0,0,.35)', 'white-space:pre-wrap',
  ].join(';')
  const head = document.createElement('div')
  head.style.cssText = 'display:flex;gap:8px;align-items:flex-start'
  const text = document.createElement('div')
  text.style.cssText = 'flex:1;min-width:0;word-break:break-word'
  text.textContent = `有个后台操作失败了，界面可能没有变化：\n${msg}`
  const close = document.createElement('button')
  close.textContent = '×'
  close.setAttribute('aria-label', '关闭')
  close.style.cssText = [
    'flex:none', 'cursor:pointer', 'border:0', 'background:transparent',
    'color:inherit', 'opacity:.6', 'font-size:16px', 'line-height:1', 'padding:0 2px',
  ].join(';')
  close.onclick = () => box.remove()
  head.append(text, close)
  box.append(head)
  document.body.append(box)
  // 12s 自动消失（用户也可能已经看到 console 了）
  window.setTimeout(() => box.remove(), 12000)
}

window.addEventListener('unhandledrejection', (ev) => {
  const reason = (ev as PromiseRejectionEvent).reason
  const msg = reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason)
  console.error('[unhandledrejection]', reason)
  notifyFailure(msg)
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
