@echo off
REM ---------------------------------------------------------------------------
REM  Makes the zip to set up (or update) the StakBridge licence server.
REM
REM  It asks which version to pack:
REM    1 = Version 1  (git branch main)
REM    2 = Version 2  (git branch v2)
REM  and packs that version's committed files, whichever branch is open now.
REM  The server builds the customers' StakBridge.exe from these files, so the
REM  version chosen here is the version your customers get.
REM
REM  Output:  server_package\StakBridgeServer-v1.zip  (or -v2.zip)
REM           server_package\StakBridgeServer\        (the same, unzipped)
REM
REM  Added on top of the git files: email_defaults.env (the mailbox settings,
REM  never in git), VERSION.txt and START_HERE.txt.
REM  Never in the package: this PC's StakBridge data (data\, data_fake\ with
REM  the MetaTrader passwords), the server's own database, logins, keys and
REM  server_settings.env, .venv, caches and the automated tests.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
    echo Git is not installed or not on PATH. Install it from https://git-scm.com and try again.
    pause
    exit /b 1
)

echo.
echo Which version do you want to put on the server?
echo   1 = Version 1  - branch main: client accounts with MetaTrader on the same PC
echo   2 = Version 2  - branch v2:   adds client accounts through the StakBridge EA
echo.
set "CHOICE="
set /p "CHOICE=Type 1 or 2 and press Enter [1]: "
if "%CHOICE%"=="" set "CHOICE=1"
set "BRANCH="
if "%CHOICE%"=="1" (
    set "BRANCH=main"
    set "VER=v1"
    set "VERNAME=Version 1"
)
if "%CHOICE%"=="2" (
    set "BRANCH=v2"
    set "VER=v2"
    set "VERNAME=Version 2"
)
if not defined BRANCH (
    echo Please type 1 or 2.
    pause
    exit /b 1
)

git rev-parse --verify --quiet %BRANCH% >nul
if errorlevel 1 (
    echo The git branch "%BRANCH%" does not exist here.
    pause
    exit /b 1
)
for /f %%C in ('git rev-parse --short %BRANCH%') do set "COMMIT=%%C"
for /f %%C in ('git log -1 --format^=%%cs %BRANCH%') do set "COMMIT_DATE=%%C"
set "CURRENT="
for /f %%B in ('git branch --show-current') do set "CURRENT=%%B"
if /i "%CURRENT%"=="%BRANCH%" (
    git diff --quiet HEAD
    if errorlevel 1 echo NOTE: %BRANCH% has changes that are not committed yet. The package has only the committed work.
)

set "PKG=%~dp0server_package"
set "OUT=%PKG%\StakBridgeServer"
set "ZIP=%PKG%\StakBridgeServer-%VER%.zip"
if exist "%PKG%" rmdir /s /q "%PKG%"
if exist "%OUT%" (
    echo The old server_package folder could not be deleted. Close any window or program
    echo that has it open, then run this again.
    pause
    exit /b 1
)
if not exist "%PKG%" mkdir "%PKG%"

echo.
echo Packing %VERNAME% (branch %BRANCH%, commit %COMMIT% of %COMMIT_DATE%) ...
git archive --format=zip -o "%PKG%\source.zip" %BRANCH%
if errorlevel 1 goto :failed
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%PKG%\source.zip' -DestinationPath '%OUT%' -Force"
if errorlevel 1 goto :failed
del "%PKG%\source.zip"

REM Only used for testing on this PC, not on the server.
if exist "%OUT%\tests" rmdir /s /q "%OUT%\tests"
for %%F in (pytest.ini run_tests.bat run_demo.bat admin.md) do if exist "%OUT%\%%F" del "%OUT%\%%F"

REM The mailbox settings are not in git (they hold the password), so they are added here.
if exist "email_defaults.env" (
    copy /y "email_defaults.env" "%OUT%\" >nul
) else (
    echo WARNING: email_defaults.env is missing in this folder. setup_server.bat will ask for the email settings.
)

> "%OUT%\VERSION.txt" (
    echo StakBridge server package
    echo %VERNAME%  - git branch %BRANCH%, commit %COMMIT% of %COMMIT_DATE%
    echo Packed on %DATE% %TIME%
)

> "%OUT%\START_HERE.txt" (
    echo StakBridge licence server - %VERNAME% - see SERVER_SETUP.md for the full guide
    echo.
    echo FIRST TIME on a new server:
    echo   1. Install Python 3.12 from python.org and tick "Add python.exe to PATH".
    echo   2. Put this StakBridgeServer folder where it should live, e.g. C:\StakBridgeServer
    echo   3. Right-click setup_server.bat - Run as administrator. Answer the questions:
    echo        SuperAdmin login, port, public address. The email settings come from
    echo        email_defaults.env: answer y to "Keep these email settings?".
    echo   4. Open the port in your VPS provider's firewall too.
    echo   5. Open http://YOUR_IP:PORT, sign in, Settings - Send Test Email.
    echo.
    echo UPDATING a server that already runs:
    echo   1. Copy everything in this folder over the server's folder, replace files.
    echo      The server keeps its database, login, keys and server_settings.env:
    echo      none of them are in this package.
    echo   2. Double-click restart_server.bat
    echo   3. Rebuild the customers' EXEs in the panel if they should get this version.
    echo.
    echo Version 2 note: the server works the same. The difference is in the StakBridge.exe
    echo it builds: v2 EXEs can also take client accounts through the StakBridge EA.
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%OUT%' -DestinationPath '%ZIP%' -Force"
if errorlevel 1 (
    echo WARNING - could not create the zip. The folder server_package\StakBridgeServer is ready to copy.
)

echo.
echo ============================================================
echo  Ready: server_package\StakBridgeServer-%VER%.zip
echo         %VERNAME%, branch %BRANCH%, commit %COMMIT%
echo ============================================================
echo  Copy the zip to the server, unzip it, then follow START_HERE.txt
echo  inside it: setup_server.bat the first time, restart_server.bat
echo  when updating.
echo.
if not defined SB_QUIET explorer "%PKG%"
pause
exit /b 0

:failed
echo.
echo FAILED - the package could not be made. Nothing was copied.
pause
exit /b 1
