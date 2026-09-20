$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script normally as the user being removed, not from an elevated Administrator window."
}

$executable = Join-Path $env:ProgramFiles "Hive IPP Bridge\HiveIPPBridge.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Hive IPP Bridge is not installed on this computer."
}

& $executable uninstall --purge
if ($LASTEXITCODE -ne 0) {
    throw "Hive IPP Bridge user removal failed with exit code $LASTEXITCODE."
}

Write-Host "The current user's Hive printer and PaperCut profile were removed."
