@echo off
setlocal
title cyberstrike-pro Electron
cd /d "%~dp0desktop"
if exist "node_modules\electron\dist\electron.exe" goto start
echo Electron dependencies are missing.
echo Run: cd desktop ^&^& npm install
pause
exit /b 1
:start
call npm start
exit /b %errorlevel%
