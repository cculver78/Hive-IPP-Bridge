param(
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $InstallerPath) {
    $besideScript = Join-Path $PSScriptRoot "HiveIPPBridge-Setup.exe"
    if (Test-Path -LiteralPath $besideScript -PathType Leaf) {
        $InstallerPath = $besideScript
    } else {
        $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
        $InstallerPath = Join-Path $repoRoot "dist\installer\HiveIPPBridge-Setup.exe"
    }
}

$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop).Path
$arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-")
$process = Start-Process -FilePath $InstallerPath -ArgumentList $arguments -Verb RunAs -Wait -PassThru

if ($process.ExitCode -ne 0) {
    throw "Hive IPP Bridge system installation failed with exit code $($process.ExitCode)."
}

$runtime = Get-Service -Name "HiveIPPBridge" -ErrorAction Stop
$provisioner = Get-Service -Name "HiveIPPBridgeProvisioner" -ErrorAction Stop
Write-Host "System installation complete."
Write-Host "  HiveIPPBridge: $($runtime.Status)"
Write-Host "  HiveIPPBridgeProvisioner: $($provisioner.Status)"
Write-Host "Each user can now run the installed Install-User.ps1 script without UAC."
