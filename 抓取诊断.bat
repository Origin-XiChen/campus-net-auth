@echo off
cd /d "%~dp0"
title CampusNetAuth - Portal Watcher

rem NOTE: keep this file pure ASCII on purpose (see 管理界面.bat header).
rem Runs diagnose_probe.py: watches the portal until YOU finish a manual
rem login, then writes probe\capture_result.json + capture_report.txt.
rem (The UI "Capture diagnostic" button uses "CampusNetAuth.exe diagnose".)

if defined CNA_DEV_PYTHON (set "PY=%CNA_DEV_PYTHON%") else set "PY=python"

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

"%PY%" diagnose_probe.py
if errorlevel 1 (
  echo.
  echo [ERROR] probe failed, see messages above.
)
echo.
pause
