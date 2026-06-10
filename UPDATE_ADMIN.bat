@echo off
if "%~1"=="WORKER" goto :worker

:: ===========================================================================
::  Hub self-update entry point. The real work runs from a TEMP copy of this
::  script: git reset rewrites UPDATE_ADMIN.bat itself, and cmd reads batch
::  files from disk line-by-line - executing the original mid-reset risks
::  running garbage. The copy is immune.
:: ===========================================================================
cd /d "%~dp0"
copy /y "%~f0" "%TEMP%\hub_update_worker.bat" >nul
start "Admin Control Hub - Update" "%TEMP%\hub_update_worker.bat" WORKER "%~dp0."
exit

:worker
set "HUB_DIR=%~2"
cd /d "%HUB_DIR%"
TITLE Admin Control Hub - Update

echo ==============================================================
echo      Admin Control Hub Self-Update (with rollback safety)
echo ==============================================================
echo.

:: Remember the current version for the rollback path
for /f %%i in ('git rev-parse HEAD') do set "OLD_SHA=%%i"
echo [INFO] Current version: %OLD_SHA%

:: 1. Git pull
echo [INFO] Pulling latest code...
git fetch origin main
git reset --hard origin/main
echo.

:: Determine the correct Python executable
set "PYTHON_CMD=python"
if exist "%HUB_DIR%\venv\Scripts\python.exe" set "PYTHON_CMD=%HUB_DIR%\venv\Scripts\python.exe"

:: 2. Update dependencies
echo [INFO] Updating dependencies...
"%PYTHON_CMD%" -m pip install -r requirements.txt --quiet
echo.

:: 3. Kill running bot + runner (project processes survive; the new runner
::    adopts them, so a hub update never interrupts the bots)
echo [INFO] Stopping running instances...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'admin_bot|runner\.py' } | ForEach-Object { try { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction Stop | Out-Null } catch {} }"
timeout /t 2 /nobreak >nul

:: 4. Relaunch (runner hidden + Hub window)
echo [INFO] Relaunching...
start "" "%HUB_DIR%\START_SERVER.bat"

:: 5. Health check: wait up to ~90s for the Hub process to come back
::    (LAUNCH_ADMIN pip-checks before starting the bot, so allow time).
echo [INFO] Waiting for the Hub to come back online...
set /a TRIES=0
:healthloop
timeout /t 10 /nobreak >nul
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'admin_bot' }; if ($p) { exit 0 } else { exit 1 }"
if %ERRORLEVEL%==0 goto :healthy
set /a TRIES+=1
echo [WAIT] Hub not up yet (check %TRIES%/9)...
if %TRIES% LSS 9 goto :healthloop

:: 6. The Hub did not come back - revert to the previous version.
echo.
echo [ROLLBACK] Hub did not come back after 90s - reverting to %OLD_SHA%...
git reset --hard %OLD_SHA%
"%PYTHON_CMD%" -m pip install -r requirements.txt --quiet
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'admin_bot|runner\.py' } | ForEach-Object { try { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction Stop | Out-Null } catch {} }"
timeout /t 2 /nobreak >nul
if not exist logs mkdir logs
echo Update at %DATE% %TIME% failed health check; reverted to %OLD_SHA% > logs\update_rollback.flag
start "" "%HUB_DIR%\START_SERVER.bat"
echo [ROLLBACK] Done. The runner will notify the admins on Telegram.
timeout /t 5 /nobreak >nul
exit

:healthy
echo [OK] Hub is back online. Update complete.
timeout /t 3 /nobreak >nul
exit
