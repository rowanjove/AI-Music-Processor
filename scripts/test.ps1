[CmdletBinding()]
param(
    [switch]$Full,
    [string]$Python = "",
    [ValidateSet("tier1", "tier2", "tier3", "all", "packaging")]
    [string]$Tier = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

if ([string]::IsNullOrWhiteSpace($Python)) {
    $bundledPython = Join-Path $projectRoot 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe'
    if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
        $python = $bundledPython
    } else {
        $sysPython = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
        if ($sysPython -and (Test-Path -LiteralPath $sysPython -PathType Leaf)) {
            Write-Warning "Bundled test Python not found ($bundledPython). Falling back to system python: $sysPython"
            $python = $sysPython
        } else {
            Write-Error "Test Python environment is missing:`n  Bundled runtime: $bundledPython (NOT FOUND)`n  System python: NOT FOUND`n[BLOCKED] Please run scripts/install_components.ps1 or specify -Python <path_to_python.exe>."
            exit 2
        }
    }
} else {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        Write-Error "Specified Python executable not found: $Python"
        exit 2
    }
    $python = $Python
}

Push-Location $projectRoot
try {
    if ($CheckOnly) {
        & $python scripts/inspect_test_tiers.py --check-env --list
        exit $LASTEXITCODE
    }

    if ($Tier -in @("tier1", "tier2", "tier3")) {
        & $python scripts/inspect_test_tiers.py --run-tier $Tier
    } elseif ($Full -or $Tier -eq "all") {
        & $python -B -m unittest discover -s tests -p 'test_*.py' -v
    } else {
        & $python -B -m unittest discover -s tests -p 'test_packaging.py' -v
    }
    $testExitCode = $LASTEXITCODE
    Write-Output "EXIT:$testExitCode"
    if ($testExitCode -ne 0) {
        exit $testExitCode
    }
} finally {
    Pop-Location
}
