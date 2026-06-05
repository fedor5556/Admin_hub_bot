@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ============================================
echo   Admin Control Hub - One-Click Installer
echo ============================================
echo.

REM --- Validate Python 3.10+ ---
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)

if !PY_MAJOR! LSS 3 (
    echo [ERROR] Python 3.10+ required. Found: !PY_VER!
    pause
    exit /b 1
)
if !PY_MAJOR! EQU 3 if !PY_MINOR! LSS 10 (
    echo [ERROR] Python 3.10+ required. Found: !PY_VER!
    pause
    exit /b 1
)
echo [OK] Python !PY_VER! detected.

REM --- Validate Git ---
where git >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Git is not installed or not in PATH.
    echo Please install Git from https://git-scm.com
    pause
    exit /b 1
)
for /f "tokens=3 delims= " %%v in ('git --version 2^>^&1') do set "GIT_VER=%%v"
echo [OK] Git !GIT_VER! detected.
echo.

REM --- Ask for install directory ---
set "DEFAULT_DIR=C:\AdminHub"
set /p "INSTALL_DIR=Install directory [!DEFAULT_DIR!]: "
if "!INSTALL_DIR!"=="" set "INSTALL_DIR=!DEFAULT_DIR!"

echo.
echo Installing to: !INSTALL_DIR!
echo.

REM --- Clone repository ---
if exist "!INSTALL_DIR!" (
    echo [WARN] Directory already exists. Pulling latest...
    cd /d "!INSTALL_DIR!"
    git pull
) else (
    echo Cloning repository...
    git clone https://github.com/fedor5556/Admin_hub_bot.git "!INSTALL_DIR!"
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Git clone failed.
        pause
        exit /b 1
    )
    cd /d "!INSTALL_DIR!"
)

REM --- Copy .env from setup package ---
if exist "%~dp0server_data\.env" (
    echo Copying .env configuration...
    copy /y "%~dp0server_data\.env" "!INSTALL_DIR!\.env" >nul
    echo [OK] .env copied.
) else (
    echo [WARN] No .env found in setup\server_data\. You must create .env manually.
    echo        See .env.example for the required variables.
)

REM --- Create virtual environment ---
echo.
echo Creating virtual environment...
python -m venv "!INSTALL_DIR!\venv"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)
echo [OK] Virtual environment created.

REM --- Install dependencies ---
echo Installing dependencies...
"!INSTALL_DIR!\venv\Scripts\pip.exe" install -r "!INSTALL_DIR!\requirements.txt"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.

REM --- Launch ---
echo.
echo ============================================
echo   Setup complete! Launching Admin Hub...
echo ============================================
echo.

call "!INSTALL_DIR!\LAUNCH_ADMIN.bat"

endlocal
pause
