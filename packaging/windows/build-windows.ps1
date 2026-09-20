param(
    [switch]$KeepBuildEnvironment
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path -Path $PSScriptRoot -ChildPath "..\..")).Path
$venv = Join-Path -Path $repoRoot -ChildPath ".venv-windows"
$python = Join-Path -Path $venv -ChildPath "Scripts\python.exe"
$dist = Join-Path -Path $repoRoot -ChildPath "dist"
$buildRoot = Join-Path -Path $repoRoot -ChildPath "build"
$work = Join-Path -Path $repoRoot -ChildPath "build\windows"
$createdVenv = $false

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "'$FilePath $($Arguments -join ' ')' failed with exit code $LASTEXITCODE."
    }
}

function Find-BasePython {
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand) {
        try {
            & $pyCommand.Source -3.12 --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return @($pyCommand.Source, "-3.12")
            }
        } catch {
            # The launcher reports an error when that specific runtime is not installed.
            # Continue with the regular python command below.
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        $versionOutput = & $pythonCommand.Source --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$versionOutput" -match "Python\s+(\d+)\.(\d+)\.") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if (($major -gt 3) -or ($major -eq 3 -and $minor -ge 12)) {
                return @($pythonCommand.Source)
            }
        }
    }

    return $null
}

try {
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    if (Test-Path -LiteralPath $venv -PathType Container) {
        throw "The existing virtual environment is incomplete at '$venv'. Remove that directory and rerun the script."
    }

    $basePython = @(Find-BasePython)
    if ($null -eq $basePython) {
        throw "No suitable CPython runtime found. Install CPython 3.12 or newer (64-bit) and rerun this script."
    }

    $createdVenv = $true
    if ($basePython.Count -eq 2) {
        Invoke-Native -FilePath $basePython[0] -Arguments @($basePython[1], "-m", "venv", $venv)
    } else {
        Invoke-Native -FilePath $basePython[0] -Arguments @("-m", "venv", $venv)
    }
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Virtual environment creation did not produce '$python'."
}

Write-Host "Using Python: $python"
Invoke-Native -FilePath $python -Arguments @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Native -FilePath $python -Arguments @("-m", "pip", "install", "-r", (Join-Path -Path $repoRoot -ChildPath "packaging\windows\requirements-build.txt"))

$env:PYTHONPATH = Join-Path -Path $repoRoot -ChildPath "src"
Invoke-Native -FilePath $python -Arguments @("-m", "unittest", "discover", "-s", (Join-Path -Path $repoRoot -ChildPath "tests"), "-v")

Invoke-Native -FilePath $python -Arguments @("-m", "PyInstaller", "--clean", "--noconfirm", "--distpath", $dist, "--workpath", $work, (Join-Path -Path $repoRoot -ChildPath "packaging\windows\HiveIPPBridge.spec"))

Write-Host "Windows build written to $(Join-Path -Path $dist -ChildPath 'HiveIPPBridge')"
}
finally {
    if (-not $KeepBuildEnvironment -and $createdVenv -and (Test-Path -LiteralPath $venv)) {
        Remove-Item -LiteralPath $venv -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Removed temporary build environment."
    }
    if (Test-Path -LiteralPath $work) {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Removed temporary PyInstaller work directory."
    }
    if (Test-Path -LiteralPath $buildRoot) {
        $remainingBuildItem = Get-ChildItem -LiteralPath $buildRoot -Force -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $remainingBuildItem) {
            Remove-Item -LiteralPath $buildRoot -Force -ErrorAction SilentlyContinue
        }
    }
}
