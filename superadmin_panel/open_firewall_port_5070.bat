@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Open Windows Firewall - Port 5070 (TCP)
echo ============================================
echo.
echo Opens inbound TCP 5070 so the SuperAdmin panel is reachable from other
echo devices (e.g. your phone) once you run it on a server/VPS. The Flask
echo app itself must also be listening on something other than 127.0.0.1 -
echo see app.py's __main__ block / SUPERADMIN_PORT for how it binds.
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "RULE_NAME=StakBridge SuperAdmin - Port 5070"

echo Removing existing rule if present...
netsh advfirewall firewall delete rule name="%RULE_NAME%" >nul 2>&1

echo Adding inbound firewall rule for TCP port 5070...
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=5070

if %errorlevel% neq 0 (
    echo ERROR: Failed to add firewall rule.
    pause
    exit /b 1
)

echo.
echo SUCCESS: Port 5070 is now open for inbound TCP connections.
echo Access from other devices: http://YOUR_SERVER_IP:5070
echo.
pause
