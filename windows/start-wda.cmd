@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start_wda.ps1"
pause
