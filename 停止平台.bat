@echo off
rem ---- 编码自举：双击时系统默认 GBK，切 UTF-8 后重新解析，保证中文不乱码 ----
chcp 65001 >nul
if not defined _CS_BOOT (
  set "_CS_BOOT=1"
  call "%~f0" %*
  exit /b %errorlevel%
)
title cyberstrike-pro 停止器
echo ============================================
echo   停止 cyberstrike-pro 后端(:8420) / 前端(:5173)
echo ============================================
echo.

rem ---- v0.64 优雅停机：先请后端自行退出（shutdown 钩子给在跑任务落现场快照），超时才硬杀 ----
set "WAITED=0"
where curl >nul 2>nul
if errorlevel 1 goto KILL_BACKEND
curl -s -m 5 -X POST http://127.0.0.1:8420/api/admin/shutdown >nul 2>nul
if errorlevel 1 goto KILL_BACKEND
echo [优雅停止] 已请求后端保存任务现场并退出，等待端口释放（最多 15 秒）...
:WAIT_LOOP
netstat -ano | findstr ":8420 " | findstr LISTENING >nul 2>nul
if errorlevel 1 goto GRACEFUL_OK
if %WAITED% GEQ 15 goto KILL_BACKEND
timeout /t 1 >nul
set /a WAITED+=1
goto WAIT_LOOP
:GRACEFUL_OK
echo [优雅停止] 后端已退出，任务现场已保存。
goto STOP_FRONTEND
:KILL_BACKEND
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8420 " ^| findstr LISTENING') do (
  echo [结束] 端口 8420 -^> PID %%a
  taskkill /F /PID %%a >nul 2>nul
)
:STOP_FRONTEND
set "KILLED=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr LISTENING') do (
  echo [结束] 端口 5173 -^> PID %%a
  taskkill /F /PID %%a >nul 2>nul
  set "KILLED=1"
)

if "%KILLED%"=="0" echo 没有发现监听 5173 的前端进程（后端已按上面步骤处理）
echo.
echo 完成，3 秒后关闭。
timeout /t 3 >nul
exit /b 0
