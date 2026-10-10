@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\device_screenshot.py"
if errorlevel 1 goto failed
start "" "%~dp0runtime\windows-device-screen.png"
exit /b 0
:failed
pause
exit /b 1
