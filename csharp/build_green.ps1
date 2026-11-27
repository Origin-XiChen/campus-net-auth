# Build the green (portable) self-contained single-file exe.
# Usage: powershell -ExecutionPolicy Bypass -File build_green.ps1 [-Version 0.7.0]
# Output: dist\green\CampusNetAuth.exe (drop-in replacement for the Python green build)
param([string]$Version = "")

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dotnetRoot = Join-Path $env:USERPROFILE ".dotnet"
$env:PATH = "$dotnetRoot;$dotnetRoot\tools;$env:PATH"
$env:DOTNET_ROOT = $dotnetRoot

$projVersion = (Select-String -Path (Join-Path $here "src\CampusNetAuth.App\CampusNetAuth.App.csproj") `
    -Pattern '<Version>([0-9A-Za-z.+-]+)</Version>').Matches[0].Groups[1].Value
if (-not $Version) { $Version = $projVersion }
Write-Host "Building green single-file CampusNetAuth $Version (project default: $projVersion)"

$out = Join-Path $here "dist\green"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }

& (Join-Path $dotnetRoot "dotnet.exe") publish (Join-Path $here "src\CampusNetAuth.App") `
    -c Release -r win-x64 --self-contained true `
    -p:Version=$Version `
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

$vi = (Get-Item $exe).VersionInfo
$numeric = ($Version -split '-')[0]
if ($vi.ProductName -ne 'CampusNetAuth') { throw "Unexpected ProductName: $($vi.ProductName)" }
if (-not $vi.FileVersion.StartsWith($numeric)) {
    throw "PE FileVersion $($vi.FileVersion) does not match requested version $Version"
}
$size = (Get-Item $exe).Length / 1MB
Write-Host ("Done: {0} ({1:N1} MB) FileVersion={2}" -f $exe, $size, $vi.FileVersion)