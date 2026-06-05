@echo off
cd /d "%~dp0"
TITLE Admin Control Hub

echo ==============================================================
echo      Starting Admin Control Hub
echo ==============================================================
echo.

:: Determine the correct Python executable
set "PYTHON_CMD=python"
if exist "venv\Scripts\python.exe" (
    set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"
    echo [INFO] Virtual environment found.
) else (
    echo [WARNING] Virtual environment not found! Using system Python.
)
echo.

:: Ensure logs directory exists
if not exist logs mkdir logs

:: Auto-install/verify all dependencies
echo [INFO] Checking dependencies...
"%PYTHON_CMD%" -m pip install -r requirements.txt --quiet
echo.

:: Launch the Admin Hub with logging
echo [LAUNCH] Starting Admin Hub Bot...
powershell -NoProfile -Command "& '%PYTHON_CMD%' -u admin_bot.py 2>&1 | Tee-Object -FilePath logs\admin_bot.log; Read-Host 'Press Enter to exit'"
