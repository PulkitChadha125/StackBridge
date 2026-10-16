@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Runs the SuperAdmin panel and restarts it if it ever exits.
rem  Started at boot by the "StakBridge SuperAdmin Server" scheduled
rem  task that setup_server.bat registers - you normally never run
rem  this yourself. Output goes to logs\server.log.
rem ============================================================

chcp 65001 >nul

if not exist "logs" mkdir "logs"

rem Load KEY=value settings written by setup_server.bat.
if exist "server_settings.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("server_settings.env") do set "%%A=%%B"
)
if not defined SERVER_PORT set "SERVER_PORT=5070"
rem 0.0.0.0 = reachable directly from the internet; 127.0.0.1 = only through
rem the HTTPS proxy (Caddy) that setup_https.bat installs.
if not defined SERVER_BIND set "SERVER_BIND=0.0.0.0"

:loop
rem Keep the log from growing forever: roll it over past ~10 MB.
if exist "logs\server.log" for %%F in ("logs\server.log") do if %%~zF GTR 10485760 move /y "logs\server.log" "logs\server.old.log" >nul

echo [%date% %time%] Starting SuperAdmin panel on port %SERVER_PORT% >> "logs\server.log"
rem The trusted-proxy flags let Caddy pass on the visitor's real IP and https
rem scheme - without them every visitor looks like 127.0.0.1, which would make
rem the per-IP login/trial rate limits block everyone at once. Only headers
rem sent from 127.0.0.1 are trusted, so direct visitors cannot spoof them.
".venv\Scripts\waitress-serve.exe" --listen=%SERVER_BIND%:%SERVER_PORT% --trusted-proxy=127.0.0.1 --trusted-proxy-count=1 "--trusted-proxy-headers=x-forwarded-for x-forwarded-host x-forwarded-proto" superadmin_panel.app:app >> "logs\server.log" 2>&1
echo [%date% %time%] Server exited with code %errorlevel% - restarting in 10 seconds >> "logs\server.log"

rem ping instead of timeout: timeout fails when there is no console (Task Scheduler).
ping -n 11 127.0.0.1 >nul
goto loop
