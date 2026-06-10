@echo off
cd /d "%~dp0"
TITLE Admin Hub - Build Virtual Environment (one-time fix)
echo ==============================================================
echo    Building the Admin Hub virtual environment
echo    (run this once, then it launches the Hub for you)
echo ==============================================================
echo.

REM --- Remove any broken/empty venv ---
if exist "venv" (
    echo [INFO] Removing old/broken venv folder...
    rmdir /s /q "venv"
)

REM --- Create venv (system python can do THIS even when locked by uv) ---
echo [INFO] Creating virtual environment...
python -m venv venv
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Retrying with the 'py' launcher...
    py -m venv venv
)
if not exist "venv\Scripts\python.exe" (
    echo.
    echo [ERROR] Could not create a virtual environment.
    echo         Install Python 3.10+ from https://python.org and tick "Add to PATH".
    pause
    exit /b 1
)
echo [OK] Virtual environment created.
echo.

REM --- Install deps INTO the venv (venv pip is NOT locked) ---
echo [INFO] Making sure pip is available...
"venv\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1
"venv\Scripts\python.exe" -m pip install --upgrade pip
echo [INFO] Installing dependencies (about a minute)...
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)
echo.
echo [OK] Dependencies installed.
echo.

REM --- Launch the Hub (venv now exists, so LAUNCH_ADMIN will use it) ---
echo [INFO] Launching the Admin Hub in a new window...
start "" "%~dp0LAUNCH_ADMIN.bat"
echo.
echo ==============================================================
echo    DONE! The Admin Hub is starting in a new window.
echo    You can close THIS window.
echo ==============================================================
pause
