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

if defined CNA_DEV_PYTHON (set "PY=%CNA_DEV_PYTHON%") else set "PY=python"

"%PY%" campusnet.py uninstall
echo.
pause
