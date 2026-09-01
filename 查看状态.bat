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

set "PY=C:\Users\XiChen\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=C:\Users\XiChen\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" campusnet.py status
echo.
echo --- last log lines ---
if exist campusnet.log "%PY%" campusnet.py log -n 15
echo.
pause
