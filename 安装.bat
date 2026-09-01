@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CampusNetAuth - Setup

rem Preferred: packaged exe (interactive setup + autostart install)
set "EXE=%~dp0CampusNetAuth.exe"
if exist "%EXE%" (
  echo ============================================================
  echo   CampusNetAuth  -  Setup
  echo ============================================================
  echo.
  "%EXE%" setup
  if errorlevel 1 goto :end
  "%EXE%" install
  goto :end
)

set "PY=C:\Users\XiChen\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=C:\Users\XiChen\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   CampusNetAuth  -  Setup
echo ============================================================
echo   Python: %PY%
echo.

"%PY%" campusnet.py setup
if errorlevel 1 goto :end

"%PY%" campusnet.py install

:end
echo.
echo ------------------------------------------------------------
pause
