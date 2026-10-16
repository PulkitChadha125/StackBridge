@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Runs Caddy (the HTTPS front door for the SuperAdmin panel) and
rem  restarts it if it ever exits. Started at boot by the
rem  "StakBridge HTTPS Proxy" scheduled task that setup_https.bat
rem  registers - you normally never run this yourself.
rem  Output goes to logs\caddy.log.
rem ============================================================

if not exist "logs" mkdir "logs"

:loop
if exist "logs\caddy.log" for %%F in ("logs\caddy.log") do if %%~zF GTR 10485760 move /y "logs\caddy.log" "logs\caddy.old.log" >nul

echo [%date% %time%] Starting Caddy >> "logs\caddy.log"
"caddy\caddy.exe" run --config "caddy\Caddyfile" --adapter caddyfile >> "logs\caddy.log" 2>&1
echo [%date% %time%] Caddy exited with code %errorlevel% - restarting in 10 seconds >> "logs\caddy.log"

rem ping instead of timeout: timeout fails when there is no console (Task Scheduler).
ping -n 11 127.0.0.1 >nul
goto loop
