@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CampusNetAuth - Status

rem NOTE: This view is also live in the UI "Status" page (auto refresh).
rem This batch stays as a CLI fallback for headless / quick checks.

rem Preferred: packaged exe (console output works natively)
set "EXE=%~dp0CampusNetAuth.exe"
if exist "%EXE%" (
  "%EXE%" status
  echo.
  echo --- last log lines ---
  if exist campusnet.log "%EXE%" log -n 15
  echo.
  pause
  exit /b 0
)

if defined CNA_DEV_PYTHON (set "PY=%CNA_DEV_PYTHON%") else set "PY=python"

"%PY%" campusnet.py status
echo.
echo --- last log lines ---
if exist campusnet.log "%PY%" campusnet.py log -n 15
echo.
pause
