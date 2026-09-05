@echo off
cd /d "%~dp0"
title CampusNetAuth - Settings

rem NOTE: keep this file pure ASCII on purpose.
rem cmd.exe relocates the batch file pointer by CHARACTER offset after a
rem codepage switch, so any multi-byte (Chinese) text here would be read at
rem the wrong byte offset and silently skipped. All Chinese UI text lives in
rem ui.py / campusnet.py instead, which are UTF-8.

rem Preferred: packaged exe (silent launch via VBS, no console flash)
set "EXE=%~dp0CampusNetAuth.exe"
set "UI_VBS=%~dp0CampusNetAuthUI.vbs"
if exist "%EXE%" (
  if exist "%UI_VBS%" (
    wscript.exe "%UI_VBS%"
    exit /b 0
  )
  start "" "%EXE%"
  exit /b 0
)

rem Fallback: dev mode via pythonw (no console window)
if defined CNA_DEV_PYTHON (set "PY=%CNA_DEV_PYTHON%") else set "PY=python"

set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"

"%PYW%" campusnet.py ui
if errorlevel 1 (
  echo.
  echo [ERROR] Could not open the settings window.
  echo Your Python build may not include tkinter.
  echo Try running this manually to see the error:
  echo   "%PY%" campusnet.py ui
  echo.
  pause
)
