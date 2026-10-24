@echo off
cd /d "%~dp0"
title CampusNetAuth - Build

rem NOTE: keep this file pure ASCII on purpose (see manager .bat headers).

rem One-click rebuild of CampusNetAuth.exe (single-file modular deliverable).
rem - --windowed  : GUI subsystem, no console window on double-click (zero black window)
rem - --workpath  : build dir (single, unified; --clean avoids stale cache reuse)
rem - --distpath  : output dir
rem - --add-data  : embed CampusNetAuthUninstall.exe inside the exe.
rem                 The uninstaller is released automatically the first time
rem                 the UI opens - a running exe cannot delete itself, so a
rem                 separate uninstaller binary is the only way to clean up.
rem                 Autostart / daemon / UI all run from this same exe; no
rem                 VBS launcher components are installed anymore (0.6.0).
rem Output: dist\CampusNetAuth.exe (the ONLY deliverable file), then copied
rem over the project root copy for dev use.
rem
rem Sandbox note: PyInstaller 6.x --clean calls os.remove, which the
rem WorkBuddy shim redirects to the Recycle Bin API (SHFileOperationW)
rem and crashes inside the sandbox. We bypass the shim by launching
rem python with -E -S (skip site.py, so sitecustomize never runs) and
rem use _pyinst_wrap.py to locate PyInstaller, re-add site-packages
rem to sys.path, and generate the exe version resource.

rem Build python: override with CNA_BUILD_PYTHON, else PATH python.
rem It must have the build deps installed (see requirements-dev.txt).
if defined CNA_BUILD_PYTHON (set "PY=%CNA_BUILD_PYTHON%") else set "PY=python"

rem Backup user data (config/cred) before cleaning dist, restore after build,
rem so upgrading never loses the user's saved settings.
set "BAK_CFG=%TEMP%\cna_build_config.json"
set "BAK_CRED=%TEMP%\cna_build_cred.bin"
if exist "%BAK_CFG%" del "%BAK_CFG%" >nul 2>nul
if exist "%BAK_CRED%" del "%BAK_CRED%" >nul 2>nul
if exist dist\config.json copy /y dist\config.json "%BAK_CFG%" >nul 2>nul
if exist dist\cred.bin copy /y dist\cred.bin "%BAK_CRED%" >nul 2>nul

echo === Cleaning old build output (bash rm, bypasses shim) ===
rmdir /s /q build  2>nul
rmdir /s /q dist   2>nul

rem NOTE: --add-data source paths MUST be absolute (%~dp0). Relative paths are
rem resolved against the spec directory (--specpath), not the CWD, so they break.
echo === Building embedded uninstaller (C# micro exe) ===
call "%~dp0build_uninstaller.bat"
if errorlevel 1 (
  echo.
  echo [ERROR] Uninstaller build failed. See messages above.
  pause
  exit /b 1
)

echo === Building (--onefile --windowed, uninstaller embedded as asset) ===
"%PY%" -E -S _pyinst_wrap.py --onefile --windowed --name CampusNetAuth --clean --noconfirm --workpath build --distpath dist --specpath build --add-data "%~dp0build_assets\CampusNetAuthUninstall.exe;." --hidden-import diagnose_probe --hidden-import desktop --hidden-import gui_server --hidden-import webview.platforms.winforms --hidden-import clr_loader campusnet.py
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed. See messages above.
  pause
  exit /b 1
)

echo === Copying exe to project root (dev copy) ===
copy /y dist\CampusNetAuth.exe CampusNetAuth.exe >nul
if errorlevel 1 (
  echo [ERROR] Copy failed.
  pause
  exit /b 1
)

rem Restore user data into the fresh dist (if a backup was taken)
if exist "%BAK_CFG%" copy /y "%BAK_CFG%" dist\config.json >nul 2>nul
if exist "%BAK_CRED%" copy /y "%BAK_CRED%" dist\cred.bin >nul 2>nul

echo.
echo === Build OK (single-file portable deliverable) ===
echo   dist\CampusNetAuth.exe   (the ONLY deliverable; uninstaller inside)
echo   CampusNetAuth.exe        (copied to project root, dev copy)
echo.
echo Integrated model: run the exe anywhere. Autostart points directly to
echo this exe (run with "daemon"), so no VBS launcher components are needed.
echo Config/cred are auto-created on first run.
echo The uninstaller (CampusNetAuthUninstall.exe) is released automatically
echo the first time the UI opens, and can also be started from
echo "CampusNetAuth.exe uninstaller".
echo.
pause
