@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Puts the SuperAdmin panel on your own domain with HTTPS,
rem  e.g. https://stakbridge.thinkstak.com. Run setup_server.bat first,
rem  and point the domain's DNS A record at this server before
rem  running this. Safe to re-run. Logic lives in setup_server.ps1.
rem ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_server.ps1" -Action Https
echo.
pause
