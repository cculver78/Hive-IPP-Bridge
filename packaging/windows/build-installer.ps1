$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$buildScript = Join-Path $PSScriptRoot "build-windows.ps1"
$installerScript = Join-Path $repoRoot "installer\windows\HiveIPPBridge.iss"

& $buildScript

$iscc = @(
    (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe")
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1

if (-not $iscc) {
    throw "Inno Setup 7 or 6 was not found. Install Inno Setup and rerun this script."
}

& $iscc $installerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}

$installer = Join-Path $repoRoot "dist\installer\HiveIPPBridge-Setup.exe"
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "The expected installer was not created at '$installer'."
}

$systemInstallScript = Join-Path $PSScriptRoot "install-system.ps1"
Copy-Item -LiteralPath $systemInstallScript -Destination (Split-Path $installer -Parent) -Force

Write-Host "Installer written to $installer"
Write-Host "Administrator script written beside the installer as install-system.ps1"
