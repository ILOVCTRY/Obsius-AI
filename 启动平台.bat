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

REM ---- 后端 core API ----
call :PORT_BUSY %API_PORT%
if errorlevel 1 (
  echo [启动] 后端 core API ...
  start "cyberstrike 后端 API :%API_PORT%" /D "%ROOT%" cmd /k ""%PY%" scripts\serve.py"
) else (
  echo [跳过] 端口 %API_PORT% 已有服务在监听，视为后端已启动
)

REM ---- 静态产物优先（desktop-app-shell M2）：webui\dist 存在=后端同源托管前端，
REM      不再起 Vite；无产物才回退 Vite dev 模式（npm run build 后自动切静态） ----
if exist "webui\dist\index.html" goto WAIT_API
echo [提示] 未找到 webui\dist（未构建），回退 Vite dev 模式；npm run build 后可走静态同源
echo.

REM ---- 检查 Node/npm ----
where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 npm，请先安装 Node.js 并加入 PATH（或先在 webui 下 npm run build 走静态托管）
  pause
  exit /b 1
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
if "%A%"=="0" if "%W%"=="0" goto READY_DEV
if %TRIES% GEQ 90 (
  echo [超时] 服务未全部就绪，请查看弹出的两个服务窗口里的报错
  echo        后端标志=%A% 前端标志=%W%（0=就绪，1=未就绪）
  pause
  exit /b 1
)
powershell -nop -c "Start-Sleep -Milliseconds 1000" >nul
goto WAITLOOP

:WAIT_API
echo [等待] 后端就绪，最多等 90 秒 ...
set /a TRIES=0
:WAITAPILOOP
set /a TRIES+=1
call :PORT_BUSY %API_PORT%
if "%ERRORLEVEL%"=="0" goto READY_STATIC
if %TRIES% GEQ 90 (
  echo [超时] 后端未就绪，请查看服务窗口里的报错
  pause
  exit /b 1
)
powershell -nop -c "Start-Sleep -Milliseconds 1000" >nul
goto WAITAPILOOP

:READY_STATIC
echo.
echo [就绪] 静态托管已随后端生效，正在打开 http://127.0.0.1:%API_PORT%
start "" "http://127.0.0.1:%API_PORT%"
goto TAIL

:READY_DEV
echo.
echo [就绪] 正在打开浏览器 http://localhost:%WEB_PORT%
start "" "http://localhost:%WEB_PORT%"

:TAIL
echo.
echo 服务各自运行在独立窗口，日志看对应黑窗口；桌面窗口模式用「启动平台（窗口）.bat」，
echo 一键结束用「停止平台.bat」。本窗口 5 秒后自动关闭。
timeout /t 5 >nul
exit /b 0

REM ============================================
REM 端口探测：后端听 IPv4 127.0.0.1，Vite 只听
REM IPv6 ::1，两个地址任一可连即视为在监听。
REM ============================================
:PORT_BUSY
powershell -nop -c "$p=%~1; foreach($s in '127.0.0.1','::1'){ try { $a=[Net.IPAddress]::Parse($s); $c=New-Object Net.Sockets.TcpClient($a.AddressFamily); $iar=$c.BeginConnect($a,$p,$null,$null); if($iar.AsyncWaitHandle.WaitOne(800,$false) -and $c.Connected){$c.Close();exit 0}; $c.Close() } catch {} }; exit 1"
exit /b %ERRORLEVEL%
