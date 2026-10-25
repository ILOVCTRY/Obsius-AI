const PROJECT_LOADING_KEY = "ui.project-loading"

export function getProjectLoadingEnabled(): boolean {
  try {
    return window.localStorage.getItem(PROJECT_LOADING_KEY) === "1"
  } catch {
    return false
  }
}

export function setProjectLoadingEnabled(enabled: boolean): boolean {
  try {
    window.localStorage.setItem(PROJECT_LOADING_KEY, enabled ? "1" : "0")
  } catch {
    // Settings should still apply for the current session when storage is restricted.
  }
  window.dispatchEvent(new CustomEvent("project-loading-setting-changed", { detail: enabled }))
  return enabled
}
