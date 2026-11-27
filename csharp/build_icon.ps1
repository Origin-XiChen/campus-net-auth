# Regenerate app icon assets from assets/icon.html (code-drawn, no watermark).
# Usage: powershell -ExecutionPolicy Bypass -File build_icon.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dotnetRoot = Join-Path $env:USERPROFILE ".dotnet"
$env:PATH = "$dotnetRoot;$env:PATH"
$env:DOTNET_ROOT = $dotnetRoot

& (Join-Path $dotnetRoot "dotnet.exe") run --project (Join-Path $here "tools\IconRenderer") -- `
    (Join-Path $here "assets\icon.html") (Join-Path $here "assets\icon-render.png") 1024
python (Join-Path $here "tools\make_assets.py")
Write-Host "Icon assets regenerated (app.ico / app-1024.png / app-256.png / splash.png)"
