@echo off
setlocal
cd /d "%~dp0"

rem Restarts the SuperAdmin server - run after editing server_settings.env.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_server.ps1" -Action Restart
echo.
pause
