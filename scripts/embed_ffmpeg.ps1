[CmdletBinding()]
param(
    [string]$SourceDirectory = 'C:\ffmpeg\bin'
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot = Join-Path $projectRoot 'runtime'
$destinationDirectory = Join-Path $runtimeRoot 'bin'
$expectedBuild = '2024-05-20-git-127ded5078-full_build-www.gyan.dev'
$expected = @{
    'ffmpeg.exe' = @{ Sha256 = '600798A69EB14C2CB099C535453EE3771E0AD22B5D78C3EE189303AABEF28EB8'; Length = 141662208 }
    'ffprobe.exe' = @{ Sha256 = 'E686329110946A0BD1B3E9A01F52F0E167526BC663D2FC418C3CCC754D5E4E15'; Length = 141495808 }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Assert-PlainDirectory([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to write through a non-directory or reparse point: $Path"
        }
    }
}

Assert-PlainDirectory $runtimeRoot
if (-not (Test-Path -LiteralPath $runtimeRoot)) {
    New-Item -ItemType Directory -Path $runtimeRoot | Out-Null
}
Assert-PlainDirectory $runtimeRoot
Assert-PlainDirectory $destinationDirectory
$runtimeFull = [System.IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\') + '\'
$destinationFull = [System.IO.Path]::GetFullPath($destinationDirectory)
if (-not $destinationFull.StartsWith($runtimeFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination escaped the runtime directory: $destinationFull"
}
if (-not (Test-Path -LiteralPath $SourceDirectory -PathType Container)) {
    throw "FFmpeg source directory does not exist: $SourceDirectory"
}

foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
    $source = Join-Path $SourceDirectory $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing source binary: $source" }
    $sourceItem = Get-Item -LiteralPath $source
    $sourceHash = Get-Sha256 $source
    if ($sourceItem.Length -ne $expected[$name].Length -or $sourceHash -ne $expected[$name].Sha256) {
        throw "Source binary differs from the accepted build: $source (size=$($sourceItem.Length), sha256=$sourceHash)"
    }
    $versionOutput = @(& $source -version 2>&1)
    $versionExitCode = $LASTEXITCODE
    $versionLine = if ($versionOutput.Count) { $versionOutput[0].ToString() } else { '' }
    if ($versionExitCode -ne 0 -or $versionLine -notmatch [regex]::Escape($expectedBuild)) {
        throw "Unexpected $name build: $versionLine"
    }
}

if (-not (Test-Path -LiteralPath $destinationDirectory)) {
    New-Item -ItemType Directory -Path $destinationDirectory | Out-Null
}

foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
    $source = Join-Path $SourceDirectory $name
    $target = Join-Path $destinationDirectory $name
    if (Test-Path -LiteralPath $target) {
        if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-Sha256 $target) -ne $expected[$name].Sha256) {
            throw "Refusing to overwrite an existing different target: $target"
        }
    } else {
        $temporary = Join-Path $destinationDirectory ('.' + $name + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
        try {
            Copy-Item -LiteralPath $source -Destination $temporary
            if ((Get-Item -LiteralPath $temporary).Length -ne $expected[$name].Length -or (Get-Sha256 $temporary) -ne $expected[$name].Sha256) {
                throw "Copied binary failed size/hash verification: $temporary"
            }
            Move-Item -LiteralPath $temporary -Destination $target
        } finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
        }
    }
    $targetVersionOutput = @(& $target -version 2>&1)
    $targetVersionExitCode = $LASTEXITCODE
    $targetVersion = if ($targetVersionOutput.Count) { $targetVersionOutput[0].ToString() } else { '' }
    if ($targetVersionExitCode -ne 0 -or $targetVersion -notmatch [regex]::Escape($expectedBuild)) {
        throw "Embedded $name failed version verification: $targetVersion"
    }
    "{0}`tbytes={1}`tsha256={2}`tversion={3}" -f $target, (Get-Item -LiteralPath $target).Length, (Get-Sha256 $target), $targetVersion
}
