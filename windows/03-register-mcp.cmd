@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\register_mcp.ps1"
set "register_exit=%errorlevel%"
pause
exit /b %register_exit%
