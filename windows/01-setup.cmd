@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup_windows.ps1"
set "setup_exit=%errorlevel%"
pause
exit /b %setup_exit%
