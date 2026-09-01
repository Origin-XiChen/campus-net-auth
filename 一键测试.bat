@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CampusNetAuth - End-to-End Test

rem NOTE: This script (e2e_test.py) is the legacy end-to-end suite.
rem The UI "Self-test" button uses "CampusNetAuth.exe test" (lighter scope).
rem Both stay; pick whichever depth you need.

set "PY=C:\Users\XiChen\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=C:\Users\XiChen\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" e2e_test.py
echo.
echo ------------------------------------------------------------
if exist e2e_result.txt type e2e_result.txt
echo.
pause
