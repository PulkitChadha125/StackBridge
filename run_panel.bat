@echo off
rem Runs the StakBridge licence server (SuperAdmin panel) on THIS PC, for trying it out.
rem For the real server on your VPS use setup_server.bat instead (see SERVER_SETUP.md).
rem   run_panel.bat           start the panel on http://127.0.0.1:5070
rem   run_panel.bat password  set or reset the SuperAdmin login
setlocal
cd /d "%~dp0"
title StakBridge licence server (local test)

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Run run.bat once first, so the Python environment exists.
  pause
  exit /b 1
)

"%PY%" -c "import flask, argon2, PyInstaller, webview" >nul 2>&1
if errorlevel 1 (
  echo Installing the licence server packages ...
  "%PY%" -m pip install --disable-pip-version-check -r requirements-server.txt
  if errorlevel 1 goto :install_failed
)

if /i "%~1"=="password" goto :login
if not exist "superadmin_panel\superadmin_config.json" goto :login
goto :run

:login
echo.
echo Choose the SuperAdmin login (you use it to open the panel).
set "SUPERADMIN_USERNAME="
set /p "SUPERADMIN_USERNAME=Username [owner]: "
if not defined SUPERADMIN_USERNAME set "SUPERADMIN_USERNAME=owner"
"%PY%" -c "import getpass, os, sys; p = getpass.getpass('Password (at least 8 characters): '); sys.exit('Too short.') if len(p) < 8 else None; sys.exit('The two passwords differ.') if getpass.getpass('Type it again: ') != p else None; os.environ['SUPERADMIN_PASSWORD'] = p; from superadmin_panel import bootstrap_superadmin as b; sys.argv = ['bootstrap']; sys.exit(b.main())"
if errorlevel 1 (
  pause
  exit /b 1
)
if /i "%~1"=="password" (
  pause
  exit /b
)

:run
set "PUBLIC_SERVER_URL=http://127.0.0.1:5070"
if exist "email_defaults.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("email_defaults.env") do set "%%A=%%B"
)
echo.
echo Licence server: http://127.0.0.1:5070   (free-trial page: http://127.0.0.1:5070/trial)
if defined SMTP_PASSWORD (
  echo Emails are sent from %SMTP_USERNAME% ^(settings in email_defaults.env^).
) else (
  echo Emails ^(trial codes, forgot password^) need the SMTP settings, see SERVER_SETUP.md.
)
echo Close this window to stop it.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5070"
"%PY%" -m superadmin_panel.app
pause
exit /b

:install_failed
echo Installing the packages failed. Check the internet connection and try again.
pause
exit /b 1
