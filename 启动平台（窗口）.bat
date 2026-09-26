@echo off
rem ---- 编码自举：双击时系统默认 GBK 代码页，先切 UTF-8 再重新解析本文件，保证中文不乱码 ----
chcp 65001 >nul
if not defined _CS_BOOT (
  set "_CS_BOOT=1"
  call "%~f0" %*
  exit /b %errorlevel%
)
title cyberstrike-pro 窗口启动器
cd /d "%~dp0"

REM ---- 选择 Python：pythonw 优先（无控制台黑窗，日志落 logs\serve-window.log），缺则退 python.exe ----
set "PYW=E:\Miniconda3\pythonw.exe"
if not exist "%PYW%" set "PYW=E:\Miniconda3\python.exe"
if not exist "%PYW%" (
  echo [错误] 找不到 E:\Miniconda3\pythonw.exe，请改用「启动平台.bat」浏览器模式
  pause
  exit /b 1
)

echo [启动] 桌面窗口模式 ...
echo   - 8420 无服务在跑：本窗口即 owner（关最后一窗 = 优雅停机并保存任务现场）
echo   - 8420 已有服务：自动作为附窗连入（关窗只关自己，可多开）
echo   - 加载地址自动探测：webui\dist 静态版 ^> Vite dev 5173 ^> 窗内指引
echo.
start "" "%PYW%" "scripts\serve.py" --window
exit /b 0
