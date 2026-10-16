@echo off
REM ---------------------------------------------------------------------------
REM  Makes a clean copy of this project to paste onto the server.
REM
REM  Output:  server_package\StakBridgeServer\
REM
REM  Copy that whole folder over the server's StakBridgeServer folder. It leaves
REM  out everything that belongs to THIS PC or to the server itself, so pasting
REM  it can never replace the server's:
REM    - database (superadmin.db), SuperAdmin login, signing keys, secret key
REM    - server_settings.env (email password), logs, built EXEs
REM  and never ships this PC's own StakBridge data (data\, data_fake\ with
REM  the encrypted broker passwords and secret.key, session.bin,
REM  license_cache.json), .venv or caches.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "OUT=%~dp0server_package\StakBridgeServer"
if exist "%~dp0server_package" rmdir /s /q "%~dp0server_package"

robocopy "%~dp0." "%OUT%" /E /NFL /NDL /NJH /NP ^
  /XD .venv .git .pytest_cache __pycache__ pycache build dist logs caddy server_package ^
      "%~dp0data" "%~dp0data_fake" ^
      "%~dp0superadmin_panel\keys" "%~dp0superadmin_panel\built_exes" "%~dp0superadmin_panel\log_uploads" "%~dp0keys" ^
  /XF *.db *.db-journal *.db-wal *.db-shm superadmin_config.json flask_secret_key.txt ^
      server_settings.env secret.key session.bin license_cache.json build_settings.json *.pyc

REM robocopy: 0-7 = success, 8+ = failure
if %ERRORLEVEL% GEQ 8 (
    echo.
    echo FAILED - robocopy error %ERRORLEVEL%. Nothing to copy.
    pause
    exit /b 1
)

REM One zip file is easier to copy over Remote Desktop than a folder.
set "ZIP=%~dp0server_package\StakBridgeServer.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%OUT%' -DestinationPath '%ZIP%' -Force"
if errorlevel 1 (
    echo.
    echo WARNING - could not create the zip. The folder is still ready to copy.
)

echo.
echo ============================================================
echo  Ready: server_package\StakBridgeServer
echo         server_package\StakBridgeServer.zip  (same thing, zipped)
echo ============================================================
echo  1. Copy StakBridgeServer.zip to the server, unzip it, and paste
echo     the StakBridgeServer folder over the server's StakBridgeServer
echo     (say Yes to replace files).
echo  2. On the server, double-click restart_server.bat
echo.
echo  The server keeps its own database, login, keys and email
echo  settings - none of them are in this package.
echo.
pause
