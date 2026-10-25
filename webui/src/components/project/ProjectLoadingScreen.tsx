import { useEffect, useState } from "react"
import { Loader2, RotateCcw } from "lucide-react"
import glassWallpaper from "@/assets/glass-wallpaper.svg"
import { getWallpaperUrl, readWallpaperSettings } from "@/lib/wallpaper"

export function ProjectLoadingScreen({
  error,
  onRetry,
  onContinue,
}: {
  error?: string
  onRetry: () => void
  onContinue: () => void
}) {
  const [wallpaper, setWallpaper] = useState<string | null>(null)
  const settings = readWallpaperSettings()

  useEffect(() => {
    let alive = true
    void getWallpaperUrl().then((url) => {
      if (alive) setWallpaper(url)
    }).catch(() => {})
    return () => { alive = false }
  }, [])

  return (
    <div className="project-loading-screen" role="status" aria-live="polite">
      <div
        className="project-loading-wallpaper"
        style={{
          backgroundImage: `url("${wallpaper ?? glassWallpaper}")`,
          backgroundSize: wallpaper ? settings.fit : "cover",
        }}
        aria-hidden="true"
      />
      <div className="project-loading-dim" aria-hidden="true" />
      <div className="project-loading-content">
        {error ? (
          <>
            <p className="project-loading-title">项目加载遇到问题</p>
            <p className="project-loading-error">{error}</p>
            <div className="project-loading-actions">
              <button type="button" onClick={onRetry}>
                <RotateCcw size={14} />重试
              </button>
              <button type="button" className="is-secondary" onClick={onContinue}>
                继续进入
              </button>
            </div>
          </>
        ) : (
          <>
            <Loader2 size={30} className="project-loading-spinner" />
            <p className="project-loading-title">正在加载项目</p>
            <p className="project-loading-caption">正在准备首屏数据…</p>
          </>
        )}
      </div>
    </div>
  )
}
