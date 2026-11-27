# Build a self-contained publish and a Velopack installer (per-user, x64).
# Usage: powershell -ExecutionPolicy Bypass -File build_installer.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dotnetRoot = Join-Path $env:USERPROFILE ".dotnet"
$env:PATH = "$dotnetRoot;$dotnetRoot\tools;$env:PATH"
$env:DOTNET_ROOT = $dotnetRoot

$version = (Select-String -Path (Join-Path $here "src\CampusNetAuth.Core\AppInfo.cs") `
    -Pattern 'Version = "([0-9A-Za-z.+-]+)"').Matches[0].Groups[1].Value
Write-Host "Packaging CampusNetAuth $version"

$publish = Join-Path $here "dist\publish"
$releases = Join-Path $here "dist\releases"
if (Test-Path $publish) { Remove-Item -Recurse -Force $publish }
if (Test-Path $releases) { Remove-Item -Recurse -Force $releases }

& (Join-Path $dotnetRoot "dotnet.exe") publish (Join-Path $here "src\CampusNetAuth.App") `
    -c Release -r win-x64 --self-contained true -o $publish

& (Join-Path $dotnetRoot "tools\vpk.exe") pack `
    --packId CampusNetAuth --packVersion $version --packTitle CampusNetAuth `
    --packAuthors "Origin-XiChen" --mainExe CampusNetAuth.exe `
    --packDir $publish --runtime win-x64 --instLocation PerUser `
    --icon (Join-Path $here "assets\app.ico") `
    --splashImage (Join-Path $here "assets\splash.png") `
    --splashProgressColor "#007AFF" `
    --outputDir $releases

Write-Host "Done. Installer: $releases\CampusNetAuth-win-Setup.exe"
