[CmdletBinding()]
param(
    [string]$OutputName = '',
    [string]$DiscoverLog = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $OutputName) {
    $version = (Get-Content -LiteralPath (Join-Path $projectRoot 'VERSION') -Raw).Trim()
    $OutputName = "AI-Music-Processor-v$version-win-x64"
}
if ($OutputName -notmatch '^[A-Za-z0-9._-]+$') {
    throw "OutputName must match ^[A-Za-z0-9._-]+$"
}
$python = Join-Path $projectRoot 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe'
$builder = Join-Path $PSScriptRoot 'build_portable.py'
$outputRoot = Join-Path $projectRoot (Join-Path 'dist' $OutputName)
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Bundled build Python is missing: $python"
}
if (Test-Path -LiteralPath $outputRoot) {
    throw "Refusing to overwrite existing task output: $outputRoot"
}
$pythonArgs = @('-B', $builder, '--output-root', $outputRoot)
if ($DiscoverLog) {
    $pythonArgs += @('--discover-log', $DiscoverLog)
}
Push-Location $projectRoot
try {
    & $python @pythonArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
