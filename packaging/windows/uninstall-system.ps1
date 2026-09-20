param(
    [switch]$PurgeUserData
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $scriptArguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($PurgeUserData) {
        $scriptArguments += " -PurgeUserData"
    }
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList $scriptArguments -Verb RunAs -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Hive IPP Bridge system removal failed with exit code $($process.ExitCode)."
    }
    exit 0
}

$installDirectory = Join-Path $env:ProgramFiles "Hive IPP Bridge"
$executable = Join-Path $installDirectory "HiveIPPBridge.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Hive IPP Bridge is not installed on this computer."
}

$removeArguments = @("uninstall", "--machine")
if ($PurgeUserData) {
    $removeArguments += "--purge"
}
& $executable @removeArguments
if ($LASTEXITCODE -ne 0) {
    throw "Hive IPP Bridge refused the system removal because a safety check failed."
}

$uninstaller = Get-ChildItem -LiteralPath $installDirectory -Filter "unins*.exe" |
    Select-Object -First 1
if ($null -eq $uninstaller) {
    throw "The Hive IPP Bridge uninstaller was not found."
}

$process = Start-Process -FilePath $uninstaller.FullName -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
) -Wait -PassThru
if ($process.ExitCode -ne 0) {
    throw "The program uninstaller failed with exit code $($process.ExitCode)."
}

if ($PurgeUserData) {
    Write-Host "System components and all encrypted user profiles were removed."
} else {
    Write-Host "System components were removed. Encrypted user profiles were preserved for reinstall."
}
