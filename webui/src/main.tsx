import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyStoredTheme } from '@/lib/theme'
import { initWallpaper } from '@/lib/wallpaper'

applyStoredTheme()
void initWallpaper()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
