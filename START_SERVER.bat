@echo off
cd /d "%~dp0"
TITLE Server Start
echo ==============================================================
echo      Starting central runner (hidden) + Admin Hub
echo ==============================================================
echo.

:: Determine the correct Python executable
set "PYTHON_CMD=python"
if exist "venv\Scripts\python.exe" set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"

:: 1. Central runner - starts every registered project's processes hidden and
::    keeps them alive. It has a single-instance guard, so double-starts are
::    harmless (the duplicate exits by itself).
echo [LAUNCH] Central runner (hidden, log: logs\runner.log)...
powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath '%PYTHON_CMD%' -ArgumentList '-u','%~dp0runner.py' -WorkingDirectory '%~dp0'"

:: 2. Admin Hub - the one visible window. Kills its own duplicates on launch.
echo [LAUNCH] Admin Hub...
start "Admin Control Hub" cmd /c LAUNCH_ADMIN.bat

echo.
echo Done. Only the Admin Hub window stays open - everything else runs hidden.
echo (Tip: put a shortcut to this file in shell:startup to survive reboots.)
pause
