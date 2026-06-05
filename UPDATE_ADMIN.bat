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

:: 3. Kill running bot
echo [INFO] Stopping running instances...
powershell -NoProfile -Command "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'admin_bot' } | ForEach-Object { $_.Terminate() | Out-Null }"
timeout /t 2 /nobreak >nul

:: 4. Relaunch
echo [INFO] Relaunching...
start "" "LAUNCH_ADMIN.bat"

echo Update complete. This window will close.
timeout /t 2 /nobreak >nul
exit
