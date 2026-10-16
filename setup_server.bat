@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  StakBridge SuperAdmin - one-click server setup for a Windows VPS.
rem  Installs everything, creates your login, saves your settings,
rem  opens the firewall, and starts the panel at every boot.
rem  Safe to re-run any time. The logic lives in setup_server.ps1.
rem ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_server.ps1" -Action Setup
echo.
pause
