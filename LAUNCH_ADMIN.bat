@echo off
cd /d "%~dp0"
TITLE Admin Control Hub

echo ==============================================================
echo      Starting Admin Control Hub
echo ==============================================================
echo.

:: Self-heal: create the virtual environment if it is missing
if not exist "venv\Scripts\python.exe" (
    echo [INFO] No virtual environment found - creating one...
    python -m venv venv
    if not exist "venv\Scripts\python.exe" py -m venv venv
)

:: Determine the correct Python executable
set "PYTHON_CMD=python"
if exist "venv\Scripts\python.exe" (
    set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"
    echo [INFO] Virtual environment found.
) else (
    echo [WARNING] Could not create venv! Falling back to system Python ^(may fail if locked by uv^).
)
echo.

:: Ensure logs directory exists
if not exist logs mkdir logs

:: Auto-install/verify dependencies (only safe inside a venv; skip on locked system python)
echo [INFO] Checking dependencies...
if exist "venv\Scripts\python.exe" "%PYTHON_CMD%" -m pip install -r requirements.txt --quiet
echo.

:: Kill any already-running Hub instance so launch is idempotent
:: (two instances = Telegram 409 Conflict + log file fights)
echo [INFO] Stopping any existing Admin Hub instance...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'admin_bot' } | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }"
echo.

:: Launch the Admin Hub. The bot writes its own rotating UTF-8 log file
:: (logs\admin_bot.log) from inside Python - do NOT pipe through Tee-Object
:: here: a second writer on the same file causes a sharing violation that
:: prevents the bot from starting at all.
echo [LAUNCH] Starting Admin Hub Bot...
"%PYTHON_CMD%" -u admin_bot.py

echo.
echo [EXIT] Admin Hub stopped. Read any error above, then press a key to close.
pause >nul
