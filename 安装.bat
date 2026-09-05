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

if defined CNA_DEV_PYTHON (set "PY=%CNA_DEV_PYTHON%") else set "PY=python"

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
