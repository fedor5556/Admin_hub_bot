@echo off
cd /d "%~dp0"
TITLE Admin Control Hub - Update

echo ==============================================================
echo      Admin Control Hub Self-Update
echo ==============================================================
echo.

:: 1. Git pull
echo [INFO] Pulling latest code...
git fetch origin main
git reset --hard origin/main
echo.

:: Determine the correct Python executable
set "PYTHON_CMD=python"
if exist "venv\Scripts\python.exe" (
    set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"
)

:: 2. Update dependencies
echo [INFO] Updating dependencies...
"%PYTHON_CMD%" -m pip install -r requirements.txt --quiet
echo.

:: 3. Kill running bot + runner (project processes survive; the new runner
::    adopts them, so a hub update never interrupts the bots)
echo [INFO] Stopping running instances...
powershell -NoProfile -Command "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'admin_bot|runner\.py' } | ForEach-Object { $_.Terminate() | Out-Null }"
timeout /t 2 /nobreak >nul

:: 4. Relaunch (runner hidden + Hub window)
echo [INFO] Relaunching...
start "" "START_SERVER.bat"

echo Update complete. This window will close.
timeout /t 2 /nobreak >nul
exit
