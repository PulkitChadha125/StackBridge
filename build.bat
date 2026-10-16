@echo off
rem Builds dist\StakBridge.exe by hand. Normally the SuperAdmin panel's "Build EXE" button does this for
rem each Admin, with the licence server address inside. A hand-built EXE needs STAKBRIDGE_LICENSE_SERVER
rem set on the PC that runs it (and license_public_key.pem from the server next to this file).
setlocal
cd /d "%~dp0"
title Build StakBridge.exe

if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run run.bat once first.
  pause
  exit /b 1
)

echo Installing the build packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements-server.txt
if errorlevel 1 (
  echo Installing the packages failed.
  pause
  exit /b 1
)

echo Building StakBridge.exe (takes a few minutes) ...
".venv\Scripts\python.exe" -m PyInstaller StakBridge.spec --clean --noconfirm
if errorlevel 1 (
  echo The build failed. See the messages above.
  pause
  exit /b 1
)

echo.
echo Done: dist\StakBridge.exe
pause
