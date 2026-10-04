@echo off
setlocal
cd /d "%~dp0desktop"
if not exist "node_modules\electron\dist\electron.exe" goto missing
call npm start
exit /b %errorlevel%
:missing
echo Electron dependencies are missing.
echo Run: cd desktop ^&^& npm install
pause
exit /b 1
