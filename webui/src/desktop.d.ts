export {}

declare global {
  interface DesktopTab { id: string; sid: string; pid?: string; type: "browser" | "terminal"; title: string; url: string }
  interface DesktopBrowserApi {
    createBrowserTab: (pid?: string, sid?: string) => Promise<DesktopTab | { error: string }>
    createTerminalTab: (pid?: string, cwd?: string) => Promise<DesktopTab | { error: string }>
    closeTab: (id: string) => Promise<void>
    activateTab: (id: string) => Promise<boolean>
    navigate: (id: string, url: string) => Promise<unknown>
    goBack: (id: string) => Promise<unknown>
    goForward: (id: string) => Promise<unknown>
    reload: (id: string) => Promise<unknown>
    openDevTools: (id: string) => Promise<void>
    setBounds: (id: string, rect: { x: number; y: number; width: number; height: number }) => void
    setBrowserVisible: (visible: boolean) => void
    listTabs: () => Promise<DesktopTab[]>
    onTabUpdated: (callback: (tabs: DesktopTab[]) => void) => () => void
    onTerminalData: (callback: (data: { id: string; data: string }) => void) => () => void
    terminalWrite: (id: string, data: string) => void
    terminalResize: (id: string, cols: number, rows: number) => void
    terminalKill: (id: string) => Promise<void>
  }
  interface Window { desktopBrowser?: DesktopBrowserApi }
  interface DesktopWindowApi {
    minimize: () => Promise<void>
    toggleMaximize: () => Promise<boolean>
    isMaximized: () => Promise<boolean>
    close: () => Promise<void>
    onMaximizedChanged: (callback: (maximized: boolean) => void) => () => void
  }
  interface Window { desktopWindow?: DesktopWindowApi }
}
