@echo off
rem ---- 编码自举：双击时系统默认 GBK 代码页，先切 UTF-8 再重新解析本文件，保证中文不乱码 ----
chcp 65001 >nul
if not defined _CS_BOOT (
  set "_CS_BOOT=1"
  call "%~f0" %*
  exit /b %errorlevel%
)
setlocal
title cyberstrike-pro 启动器
cd /d "%~dp0"
set "ROOT=%cd%"
set "API_PORT=8420"
set "WEB_PORT=5173"

echo ============================================
echo   cyberstrike-pro 一键启动
echo   后端 API : http://127.0.0.1:%API_PORT%
echo   前端页面 : http://localhost:%WEB_PORT%
echo ============================================
echo.

REM ---- 选择 Python：优先本机 Miniconda，其次 PATH ----
set "PY=python"
if exist "E:\Miniconda3\python.exe" set "PY=E:\Miniconda3\python.exe"
"%PY%" --version >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到可用的 Python，已试 E:\Miniconda3\python.exe 和 PATH 中的 python
  pause
  exit /b 1
)

REM ---- 检查 Node/npm ----
where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 npm，请先安装 Node.js 并加入 PATH
  pause
  exit /b 1
)

REM ---- 后端 core API ----
call :PORT_BUSY %API_PORT%
if errorlevel 1 (
  echo [启动] 后端 core API ...
  start "cyberstrike 后端 API :%API_PORT%" /D "%ROOT%" cmd /k ""%PY%" scripts\serve.py"
) else (
  echo [跳过] 端口 %API_PORT% 已有服务在监听，视为后端已启动
)

REM ---- 前端依赖（仅首次） ----
if not exist "webui\node_modules" (
  echo [安装] 首次运行，安装 webui 依赖，可能需要几分钟 ...
  pushd "%ROOT%\webui"
  call npm.cmd install
  popd
)

REM ---- 前端 Vite（strictPort：端口被占直接报错，不静默漂到 5174） ----
call :PORT_BUSY %WEB_PORT%
if errorlevel 1 (
  echo [启动] 前端 Vite ...
  start "cyberstrike 前端 Vite :%WEB_PORT%" /D "%ROOT%\webui" cmd /k "npm.cmd run dev -- --port %WEB_PORT% --strictPort"
) else (
  echo [跳过] 端口 %WEB_PORT% 已有服务在监听，视为前端已启动
)

echo.
echo [等待] 服务就绪，最多等 90 秒 ...
set /a TRIES=0
:WAITLOOP
set /a TRIES+=1
call :PORT_BUSY %API_PORT%
set "A=%ERRORLEVEL%"
call :PORT_BUSY %WEB_PORT%
set "W=%ERRORLEVEL%"
if "%A%"=="0" if "%W%"=="0" goto READY
if %TRIES% GEQ 90 (
  echo [超时] 服务未全部就绪，请查看弹出的两个服务窗口里的报错
  echo        后端标志=%A% 前端标志=%W%（0=就绪，1=未就绪）
  pause
  exit /b 1
)
powershell -nop -c "Start-Sleep -Milliseconds 1000" >nul
goto WAITLOOP

:READY
echo.
echo [就绪] 正在打开浏览器 http://localhost:%WEB_PORT%
start "" "http://localhost:%WEB_PORT%"
echo.
echo 两个服务各自运行在独立窗口，日志看那两个黑窗口；关掉对应窗口即停止该服务，
echo 也可以双击项目根目录的「停止平台.bat」一键结束。本窗口 5 秒后自动关闭。
timeout /t 5 >nul
exit /b 0

REM ============================================
REM 端口探测：后端听 IPv4 127.0.0.1，Vite 只听
REM IPv6 ::1，两个地址任一可连即视为在监听。
REM ============================================
:PORT_BUSY
powershell -nop -c "$p=%~1; foreach($s in '127.0.0.1','::1'){ try { $a=[Net.IPAddress]::Parse($s); $c=New-Object Net.Sockets.TcpClient($a.AddressFamily); $iar=$c.BeginConnect($a,$p,$null,$null); if($iar.AsyncWaitHandle.WaitOne(800,$false) -and $c.Connected){$c.Close();exit 0}; $c.Close() } catch {} }; exit 1"
exit /b %ERRORLEVEL%
