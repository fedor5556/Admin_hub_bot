@echo off
cd /d "%~dp0"

echo Stopping Admin Control Hub...
echo.

REM --- Find and kill python processes running admin_bot ---
powershell -Command "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*admin_bot*' } | ForEach-Object { Write-Host ('Killing PID ' + $_.ProcessId + ': ' + $_.CommandLine); $_.Terminate() | Out-Null }"

echo.
echo Done.
pause
