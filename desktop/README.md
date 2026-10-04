# Electron desktop shell

This shell hosts the React UI in a `BrowserWindow` and renders each browser
tab as a real Chromium `WebContentsView`. The Python API attaches to those
pages through the Electron remote debugging endpoint, so AI actions and human
input use the same page.

```powershell
cd desktop
npm install
npm start
```

`node-pty` starts PowerShell tabs with the project root as their working
directory. Set `CS_UI_URL` to use the Vite dev server and `CS_PROJECT_ID` to
select the project used by new browser tabs.
