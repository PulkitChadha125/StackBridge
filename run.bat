@echo off
rem StakBridge launcher.
rem   run.bat           start with real MT5 terminals (data folder: data)
rem   run.bat fake      start with the simulated broker (data folder: data_fake)
rem   run.bat test      run the automated tests
rem   run.bat password  set or reset the web page password
rem   run.bat token     create an API token for a program that sends trades
rem   run.bat install   reinstall the Python packages, then start
rem Add "fake" after password/token to change the demo instead, e.g. run.bat password fake
setlocal
cd /d "%~dp0"
title StakBridge

set "PY=%~dp0.venv\Scripts\python.exe"
set "MODE=%~1"

if not exist "%PY%" (
  echo Creating the Python environment in .venv ...
  python -m venv .venv
  if errorlevel 1 goto :no_python
  set "NEED_INSTALL=1"
)

"%PY%" -c "import fastapi, uvicorn, cryptography, pytest" >nul 2>&1
if errorlevel 1 set "NEED_INSTALL=1"
if /i "%MODE%"=="install" set "NEED_INSTALL=1"

if defined NEED_INSTALL (
  echo Installing packages from requirements.txt ...
  "%PY%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto :install_failed
)

if /i "%MODE%"=="test" (
  "%PY%" -m pytest -v
  pause
  exit /b
)

set "FAKE="
if /i "%MODE%"=="fake" set "FAKE=1"
if /i "%~2"=="fake" set "FAKE=1"
if defined FAKE (
  set "STAKBRIDGE_BACKEND=fake"
  if not defined STAKBRIDGE_DATA_DIR set "STAKBRIDGE_DATA_DIR=%~dp0data_fake"
  echo.
  echo   SIMULATED BROKER MODE - no real MT5 orders are sent.
  echo.
)

if /i "%MODE%"=="password" (
  "%PY%" -m stakbridge set-password
  pause
  exit /b
)
if /i "%MODE%"=="token" (
  "%PY%" -m stakbridge create-token --label "created with run.bat"
  pause
  exit /b
)

if not defined STAKBRIDGE_PORT set "STAKBRIDGE_PORT=8000"
rem Open the browser a few seconds after the server starts.
start "" /min cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:%STAKBRIDGE_PORT%"
"%PY%" -m stakbridge serve --port %STAKBRIDGE_PORT%
echo.
echo StakBridge stopped.
pause
exit /b

:no_python
echo Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/ and tick "Add to PATH".
pause
exit /b 1

:install_failed
echo Installing the packages failed. Check your internet connection and try: run.bat install
pause
exit /b 1
