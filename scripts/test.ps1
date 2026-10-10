[CmdletBinding()]
param(
    [switch]$Full
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $projectRoot 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Bundled test Python is missing: $python"
}
Push-Location $projectRoot
try {
    if ($Full) {
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
