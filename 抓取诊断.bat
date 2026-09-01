@echo off
cd /d "%~dp0"
title CampusNetAuth - Portal Watcher

rem NOTE: keep this file pure ASCII on purpose (see 管理界面.bat header).
rem NOTE: This script is the legacy portal-watcher (probe\capture_probe.py).
rem The UI "Capture diagnostic" button uses "CampusNetAuth.exe diagnose".
rem Both write to probe\, but use different methods. Use whichever fits.

set "PY=C:\Users\XiChen\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=C:\Users\XiChen\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo  CampusNetAuth Portal Watcher
echo  It watches the portal until YOU finish a manual login,
echo  then writes probe\capture_result.json + capture_report.txt.
echo  No password is ever touched or written.
echo ============================================================
echo.
echo  Next: open the portal login page in your browser and log in.
echo  Press Ctrl+C here to stop watching.
echo.

"%PY%" probe\capture_probe.py
if errorlevel 1 (
  echo.
  echo [ERROR] probe failed, see messages above.
)
echo.
pause
