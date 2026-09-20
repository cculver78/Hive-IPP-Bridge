$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script normally as the user being enrolled, not from an elevated Administrator window."
}

$executable = Join-Path $env:ProgramFiles "Hive IPP Bridge\HiveIPPBridge.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Hive IPP Bridge is not installed. Ask an administrator to run install-system.ps1 first."
}

& $executable setup
if ($LASTEXITCODE -ne 0) {
    throw "Hive IPP Bridge user setup failed with exit code $LASTEXITCODE."
}

& $executable status
if ($LASTEXITCODE -ne 0) {
    throw "Hive IPP Bridge user setup completed but status verification failed."
}
