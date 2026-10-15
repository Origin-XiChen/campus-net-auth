@echo off
cd /d "%~dp0"
rem ===========================================================================
rem  Build the embedded uninstaller (CampusNetAuthUninstall.exe).
rem
rem  Why a separate tiny .NET exe instead of another PyInstaller onefile:
rem    * ~15KB instead of ~8-19MB (it gets embedded INSIDE the main exe,
rem      so every MB here doubles the download);
rem    * no %TEMP% self-extraction, so the uninstaller itself never leaves
rem      a _MEI residue, and it can delete the main exe while running;
rem    * .NET Framework 4.x and csc.exe ship with Windows 10/11, so no extra
rem      toolchain, no third-party runtime for the user.
rem
rem  Source : uninstaller\CampusNetAuthUninstall.cs  (UTF-8, compiled with
rem           /codepage:65001 so the Chinese UI strings survive)
rem  Output : build_assets\CampusNetAuthUninstall.exe
rem
rem  NOTE: keep this file pure ASCII (see the manager .bat headers).
rem  Override the compiler with CNA_CSC if csc.exe lives elsewhere.
rem ===========================================================================
setlocal
set "OUTDIR=%~dp0build_assets"
set "OUT=%OUTDIR%\CampusNetAuthUninstall.exe"
set "SRC=%~dp0uninstaller\CampusNetAuthUninstall.cs"

if not exist "%SRC%" (
  echo [ERROR] Source not found: %SRC%
  exit /b 1
)

set "CSC=%CNA_CSC%"
if not defined CSC if exist "%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not defined CSC if exist "%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not defined CSC (
  for /f "delims=" %%D in ('dir /b /o-n "%WINDIR%\Microsoft.NET\Framework64\v4*" 2^>nul') do (
    if not defined CSC if exist "%WINDIR%\Microsoft.NET\Framework64\%%D\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework64\%%D\csc.exe"
  )
)
if not defined CSC (
  echo [ERROR] csc.exe ^(C# compiler^) not found.
  echo         It ships with the .NET Framework 4.x, normally at
  echo         %%WINDIR%%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
  echo         Point CNA_CSC at another csc.exe if yours lives elsewhere.
  exit /b 1
)

if not exist "%OUTDIR%" mkdir "%OUTDIR%"
echo === Building uninstaller with "%CSC%" ===
"%CSC%" /nologo /target:winexe /platform:anycpu /optimize+ /codepage:65001 ^
  /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll ^
  /out:"%OUT%" "%SRC%"
if errorlevel 1 (
  echo.
  echo [ERROR] Uninstaller build failed. See messages above.
  exit /b 1
)
if not exist "%OUT%" (
  echo [ERROR] Compiler reported success but "%OUT%" is missing.
  exit /b 1
)
for %%F in ("%OUT%") do echo === Uninstaller OK: %%~nxF  %%~zF bytes ===
endlocal
exit /b 0