# Build the green (portable) self-contained single-file exe.
# Usage: powershell -ExecutionPolicy Bypass -File build_green.ps1
# Output: dist\green\CampusNetAuth.exe (drop-in replacement for the Python green build)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dotnetRoot = Join-Path $env:USERPROFILE ".dotnet"
$env:PATH = "$dotnetRoot;$dotnetRoot\tools;$env:PATH"
$env:DOTNET_ROOT = $dotnetRoot

$appVersion = (Select-String -Path (Join-Path $here "src\CampusNetAuth.Core\AppInfo.cs") `
    -Pattern 'Version = "([0-9.]+)"').Matches[0].Groups[1].Value
$projVersion = (Select-String -Path (Join-Path $here "src\CampusNetAuth.App\CampusNetAuth.App.csproj") `
    -Pattern '<Version>([0-9.]+)</Version>').Matches[0].Groups[1].Value
if ($appVersion -ne $projVersion) {
    throw "Version mismatch: AppInfo.cs=$appVersion, CampusNetAuth.App.csproj=$projVersion (PE version would differ from --version)"
}
Write-Host "Building green single-file CampusNetAuth $appVersion"

$out = Join-Path $here "dist\green"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }

& (Join-Path $dotnetRoot "dotnet.exe") publish (Join-Path $here "src\CampusNetAuth.App") `
    -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:EnableCompressionInSingleFile=true `
    -p:DebugType=None -p:DebugSymbols=false `
    -o $out

$exe = Join-Path $out "CampusNetAuth.exe"
# 绿色交付只需要单文件：清掉 SDK 顺带复制的 XML 文档等
Get-ChildItem -LiteralPath $out -File |
    Where-Object { $_.Name -ne 'CampusNetAuth.exe' } |
    Remove-Item -Force
$size = (Get-Item $exe).Length / 1MB
Write-Host ("Done: {0} ({1:N1} MB)" -f $exe, $size)