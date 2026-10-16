@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\install_signed_wda.py" --check-signed
if not errorlevel 1 goto signed
if exist "runtime\signing-service.txt" set /p WDA_ANISETTE_URL=<"runtime\signing-service.txt"
if not exist "bin\wda-installer.exe" goto missing
"bin\wda-installer.exe" "%~dp0."
set "install_exit=%errorlevel%"
if not "%install_exit%"=="0" goto retry_signed
".venv\Scripts\python.exe" "scripts\install_signed_wda.py" --verify-only
set "install_exit=%errorlevel%"
pause
exit /b %install_exit%
:retry_signed
".venv\Scripts\python.exe" "scripts\install_signed_wda.py" --check-signed
if errorlevel 1 goto signing_failed
echo Retrying the existing signed bundle through pymobiledevice3...
goto signed
:signing_failed
pause
exit /b %install_exit%
:signed
".venv\Scripts\python.exe" "scripts\install_signed_wda.py"
set "install_exit=%errorlevel%"
pause
exit /b %install_exit%
:missing
echo Installer has not been built. Run scripts\build_installer.ps1 first.
pause
exit /b 1
