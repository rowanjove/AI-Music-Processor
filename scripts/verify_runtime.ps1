[CmdletBinding()]
param(
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 900,
    [string]$DataRoot
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $projectRoot 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe'
$application = Join-Path $projectRoot 'AI_Music_Processor.pyw'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Bundled Python is missing: $python"
}
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw "Application entry point is missing: $application"
}

$arguments = @('-B', $application, '--verify-runtime', '--timeout-seconds', [string]$TimeoutSeconds)
if ($DataRoot) {
    $arguments += @('--data-root', $DataRoot)
}
& $python @arguments
$verifyExitCode = $LASTEXITCODE
if ($verifyExitCode -ne 0) {
    exit $verifyExitCode
}
