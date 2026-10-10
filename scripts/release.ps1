[CmdletBinding()]
param(
    [string]$OutputName = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildScript = Join-Path $PSScriptRoot 'build.ps1'
Write-Output 'Building a local acceptance ZIP only; publication is not part of this script.'
if ($OutputName) {
    & $buildScript -OutputName $OutputName
} else {
    & $buildScript
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
