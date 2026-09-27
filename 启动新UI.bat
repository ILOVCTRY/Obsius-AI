@echo off
chcp 65001 >nul
if not defined _CS_NEW_UI_BOOT (
  set "_CS_NEW_UI_BOOT=1"
  call "%~f0" %*
  exit /b %errorlevel%
)
setlocal
set "ROOT=%~dp0"
set "API_PORT=8420"
set "WEB_PORT=5173"
set "PY=E:\Miniconda3\python.exe"
if not exist "%PY%" set "PY=python"

title cyberstrike-pro 新 UI 启动器
cd /d "%ROOT%"

echo ============================================
echo   cyberstrike-pro 新 UI
echo   后端 API: http://127.0.0.1:%API_PORT%
echo   前端 UI : http://localhost:%WEB_PORT%
echo ============================================
echo.

"%PY%" --version >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到可用的 Python，请确认已安装 Python 或 E:\Miniconda3\python.exe
  pause
  exit /b 1
)

powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try {$c.Connect('127.0.0.1',%API_PORT%); $c.Close(); exit 0} catch {exit 1}" >nul 2>nul
if errorlevel 1 (
  echo [启动] 后端 API...
  start "cyberstrike 后端 API :%API_PORT%" /D "%ROOT%" cmd /k ""%PY%" scripts\serve.py"
) else (
  echo [跳过] 后端 API 已在端口 %API_PORT% 运行
)

where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 npm，请先安装 Node.js 并加入 PATH
  pause
  exit /b 1
)

if not exist "%ROOT%webui\node_modules" (
  echo [安装] 正在安装前端依赖...
  pushd "%ROOT%webui"
  call npm.cmd install
  if errorlevel 1 (
    popd
    echo [错误] 前端依赖安装失败
    pause
    exit /b 1
  )
  popd
)

powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try {$c.Connect('127.0.0.1',%WEB_PORT%); $c.Close(); exit 0} catch {exit 1}" >nul 2>nul
if errorlevel 1 (
  echo [启动] 新 UI Vite 开发服务器...
  start "cyberstrike 新 UI :%WEB_PORT%" /D "%ROOT%webui" cmd /k "npm.cmd run dev -- --host localhost --port %WEB_PORT% --strictPort"
) else (
  echo [跳过] 前端已在端口 %WEB_PORT% 运行
)

echo.
echo [等待] 正在等待新 UI 启动...
timeout /t 3 /nobreak >nul
start "" "http://localhost:%WEB_PORT%"
echo [就绪] 已打开新 UI，服务窗口将保持运行。
echo.
endlocal
exit /b 0