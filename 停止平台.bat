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

set "KILLED=0"
for %%P in (8420 5173) do (
  for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P " ^| findstr LISTENING') do (
    echo [结束] 端口 %%P -^> PID %%a
    taskkill /F /PID %%a >nul 2>nul
    set "KILLED=1"
  )
)

if "%KILLED%"=="0" echo 没有发现监听 8420/5173 的进程，可能本来就没启动
echo.
echo 完成，3 秒后关闭。
timeout /t 3 >nul
exit /b 0
