@echo off
cd /d "%~dp0"
echo Stopping the central runner...
echo.

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'runner\.py' } | ForEach-Object { Write-Host ('Killing PID ' + $_.ProcessId); try { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction Stop | Out-Null } catch {} }"

echo.
echo Done. NOTE: the managed project processes were NOT stopped - they keep
echo running. Stop them via the Admin Hub or each project's STOP_ALL.bat.
pause
