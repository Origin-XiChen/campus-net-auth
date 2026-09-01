@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CampusNetAuth - Uninstall

rem NOTE: This function is also available in the UI "Automation" page
rem (toggle "Auto-auth after login"). This batch stays as a CLI fallback.
rem Recommended: run dist\CampusNetAuth.exe and use the UI.

rem Preferred: packaged exe
set "EXE=%~dp0CampusNetAuth.exe"
if exist "%EXE%" (
  "%EXE%" uninstall
  echo.
  pause
  exit /b 0
)

set "PY=C:\Users\XiChen\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=C:\Users\XiChen\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" campusnet.py uninstall
echo.
pause
