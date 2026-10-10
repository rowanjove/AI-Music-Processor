[CmdletBinding()]
param(
    [string]$Root,
    [string[]]$Components = @("all"),
    [string[]]$OfflinePackage = @(),
    [string]$CachePath,
    [string]$PythonPath,
    [switch]$InstallPython,
    [switch]$List,
    [switch]$Plan,
    [switch]$CacheOnly,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
if ([string]::IsNullOrWhiteSpace($Root)) { $Root = Split-Path -Parent $scriptRoot }
$Root = [IO.Path]::GetFullPath($Root)
$ManifestPath = Join-Path $Root "packaging\download-sources.json"
$HelperPath = Join-Path $Root "core\online_components.py"
$VersionPath = Join-Path $Root "VERSION"
$UserDataRoot = if ($env:AI_MUSIC_PROCESSOR_DATA_ROOT) { [IO.Path]::GetFullPath($env:AI_MUSIC_PROCESSOR_DATA_ROOT) } else { Join-Path $env:LOCALAPPDATA "AI-Music-Processor" }
$ReceiptRoot = Join-Path $UserDataRoot "logs"
$ProgressPath = Join-Path $ReceiptRoot "online-components-progress.jsonl"

function Write-ProgressEvent {
    param([hashtable]$Event)
    $Event.timestamp_utc = [DateTime]::UtcNow.ToString("o")
    $line = $Event | ConvertTo-Json -Compress -Depth 12
    $parent = Split-Path -Parent $ProgressPath
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Add-Content -LiteralPath $ProgressPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Get-SourceManifest {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "缺少下载来源清单：$ManifestPath"
    }
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.policy.transport -ne "https-only-with-explicit-hosts") {
        throw "下载来源清单策略无效"
    }
    return $manifest
}

function Test-TkPython {
    param([string]$Candidate)
    if ([string]::IsNullOrWhiteSpace($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    try {
        $probe = & $Candidate -B -c "import tkinter,sys,platform; assert platform.python_version()=='3.9.13'; print(sys.executable)" 2>$null
        return ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($probe | Select-Object -Last 1)))
    } catch {
        return $false
    }
}

function Find-TkPython {
    $candidates = @()
    if ($PythonPath) { $candidates += $PythonPath }
    $bundled = Join-Path $Root "tools\RVC\RVC-beta-v2-0618\runtime\python.exe"
    $candidates += $bundled
    $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python39\python.exe")
    try {
        $py = Get-Command py -ErrorAction Stop
        $reported = & $py.Source -3.9 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0) { $candidates += ($reported | Select-Object -Last 1) }
    } catch { }
    try {
        $python = Get-Command python -ErrorAction Stop
        $candidates += $python.Source
    } catch { }
    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        $resolved = try { (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path } catch { $candidate }
        if (Test-TkPython $resolved) { return $resolved }
    }
    return $null
}

function Get-VerifiedFile {
    param(
        [string]$Url,
        [string]$Destination,
        [string]$ExpectedSha256,
        [int]$ExpectedBytes
    )
    if ($Url -notmatch '^https://') { throw "只允许 HTTPS 来源" }
    if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') { throw "来源缺少有效 SHA-256" }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $partial = "$Destination.download.part"
    $temp = "$Destination.download.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        Write-ProgressEvent @{ event = "download_start"; url = $Url; destination = $Destination; expected_bytes = $ExpectedBytes }
        Invoke-WebRequest -Uri $Url -OutFile $temp -UseBasicParsing
        $item = Get-Item -LiteralPath $temp
        if ($item.Length -ne $ExpectedBytes) { throw "下载大小不匹配：$($item.Length) != $ExpectedBytes" }
        $hash = (Get-FileHash -LiteralPath $temp -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $ExpectedSha256) { throw "下载 SHA-256 不匹配" }
        Move-Item -LiteralPath $temp -Destination $Destination -Force
        Write-ProgressEvent @{ event = "download_complete"; destination = $Destination; bytes = $item.Length; sha256 = $hash }
        return $Destination
    } catch {
        Write-ProgressEvent @{ event = "download_failed"; destination = $Destination; error = $_.Exception.Message }
        throw
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    }
}

function Ensure-BootstrapPython {
    $python = Find-TkPython
    if ($python) { return $python }
    if (-not $InstallPython) {
        throw "未找到带 Tcl/Tk 的 Python 3.9。请先安装官方 Python 3.9.13，或重新运行并加 -InstallPython。"
    }
    $manifest = Get-SourceManifest
    $source = $manifest.sources | Where-Object { $_.component_id -eq "runtime.python" }
    if ($source.status -ne "PINNED" -or $source.kind -ne "python_installer") {
        throw "Python 来源没有经过 SHA-256 发布门禁"
    }
    $cache = if ($CachePath) { [IO.Path]::GetFullPath($CachePath) } else { Join-Path $Root ".component-cache" }
    $installer = Join-Path $cache "python-$($source.version)-amd64.exe"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf) -or
        (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $source.sha256) {
        Get-VerifiedFile -Url $source.urls[0] -Destination $installer -ExpectedSha256 $source.sha256 -ExpectedBytes $source.size_bytes | Out-Null
    }
    Write-ProgressEvent @{ event = "python_install_start"; version = $source.version; installer = $installer }
    $arguments = @("/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0", "Include_tcltk=1", "Include_test=0")
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "官方 Python 安装器失败，退出码 $($process.ExitCode)" }
    $python = Find-TkPython
    if (-not $python) { throw "Python 安装完成但 Tk 探针失败；拒绝继续安装组件" }
    Write-ProgressEvent @{ event = "python_install_complete"; executable = $python }
    return $python
}

function Assert-RuntimeMutationPath {
    param([string]$Path, [string]$Parent)
    $absolute = [IO.Path]::GetFullPath($Path)
    $parentAbsolute = [IO.Path]::GetFullPath($Parent).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $rootAbsolute = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not $absolute.StartsWith($parentAbsolute + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        -not $parentAbsolute.StartsWith($rootAbsolute + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "runtime 文件操作路径越界"
    }
    $current = $absolute
    while ($current.Length -ge $rootAbsolute.Length) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "runtime 路径含重解析点" }
        }
        if ($current.Equals($rootAbsolute, [StringComparison]::OrdinalIgnoreCase)) { break }
        $current = Split-Path -Parent $current
        if ([string]::IsNullOrWhiteSpace($current)) { throw "runtime 路径验证失败" }
    }
}

function Ensure-RuntimeLayout {
    param([string]$Python)
    $runtime = Join-Path $Root "tools\RVC\RVC-beta-v2-0618\runtime"
    $runtimePython = Join-Path $runtime "python.exe"
    if (Test-Path -LiteralPath $runtimePython -PathType Leaf) {
        if (-not (Test-TkPython $runtimePython)) { throw "现有 runtime/python.exe 缺少 Tcl/Tk 探针" }
        return $runtimePython
    }
    # A Windows venv puts its executable under Scripts\python.exe and keeps
    # an absolute home= entry in pyvenv.cfg.  Copy the official host install
    # into the application layout instead, so runtime/python.exe and its DLLs,
    # stdlib and Tcl/Tk stay together after the user moves the GitHub checkout.
    $hostRoot = (& $Python -B -c "import pathlib,sys; print(pathlib.Path(sys.base_prefix).resolve())" 2>$null | Select-Object -Last 1).Trim()
    if ([string]::IsNullOrWhiteSpace($hostRoot) -or -not (Test-Path -LiteralPath (Join-Path $hostRoot "python.exe") -PathType Leaf)) {
        throw "无法定位带 Tcl/Tk 的 Python 主机安装目录"
    }
    $hostRoot = (Resolve-Path -LiteralPath $hostRoot).Path
    $runtimeParent = Split-Path -Parent $runtime
    $expectedParent = [IO.Path]::GetFullPath((Join-Path $Root "tools\RVC\RVC-beta-v2-0618"))
    $resolvedParent = [IO.Path]::GetFullPath($runtimeParent)
    if (-not $resolvedParent.Equals($expectedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "runtime 父目录不符合应用布局"
    }
    New-Item -ItemType Directory -Force -Path $runtimeParent | Out-Null
    $staging = Join-Path $runtimeParent (".runtime-stage-" + [Guid]::NewGuid().ToString("N"))
    $backup = Join-Path $runtimeParent (".runtime-backup-" + [Guid]::NewGuid().ToString("N"))
    Assert-RuntimeMutationPath -Path $runtime -Parent $runtimeParent
    Assert-RuntimeMutationPath -Path $staging -Parent $runtimeParent
    Assert-RuntimeMutationPath -Path $backup -Parent $runtimeParent
    $movedNew = $false
    $movedOld = $false
    $resolvedStaging = [IO.Path]::GetFullPath($staging)
    if (-not $resolvedStaging.StartsWith($resolvedParent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "runtime 暂存目录越界"
    }
    try {
        New-Item -ItemType Directory -Force -Path $staging | Out-Null
        # Keep the standard library, DLLs and Tcl/Tk, but do not clone a
        # vendor/RVC host's multi-gigabyte third-party site-packages.  pip and
        # setuptools are retained so the pinned wheels can be installed next.
        Get-ChildItem -LiteralPath $hostRoot -Force | Where-Object {
            ($_.PSIsContainer -and ($_.Name -in @("DLLs", "tcl"))) -or (-not $_.PSIsContainer -and ($_.Extension -in @(".exe", ".dll", ".pyd", ".zip", ".pth", "._pth", ".txt", ".cat")))
        } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $staging -Recurse -Force
        }
        $sourceLib = Join-Path $hostRoot "Lib"
        if (-not (Test-Path -LiteralPath $sourceLib -PathType Container)) { throw "Python 主机缺少标准库 Lib" }
        $targetLib = Join-Path $staging "Lib"
        New-Item -ItemType Directory -Force -Path $targetLib | Out-Null
        Get-ChildItem -LiteralPath $sourceLib -Force | Where-Object { $_.Name -ne "site-packages" } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $targetLib -Recurse -Force
        }
        $sourceSite = Join-Path $hostRoot "Lib\site-packages"
        if (Test-Path -LiteralPath $sourceSite -PathType Container) {
            $targetSite = Join-Path $staging "Lib\site-packages"
            New-Item -ItemType Directory -Force -Path $targetSite | Out-Null
            $bootstrapNames = @("pip", "setuptools", "pkg_resources", "_distutils_hack", "easy_install.py", "tkinter")
            Get-ChildItem -LiteralPath $sourceSite -Force | Where-Object {
                $name = $_.Name
                ($bootstrapNames -contains $name) -or $name.StartsWith("pip-", [StringComparison]::OrdinalIgnoreCase) -or $name.StartsWith("setuptools-", [StringComparison]::OrdinalIgnoreCase)
            } | ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination $targetSite -Recurse -Force
            }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $staging "python.exe") -PathType Leaf)) {
            throw "Python 主机复制后缺少 python.exe"
        }
        if (-not (Test-TkPython (Join-Path $staging "python.exe"))) { throw "暂存 Python 标准库/Tk 探针失败" }
        Assert-RuntimeMutationPath -Path $runtime -Parent $runtimeParent
        Assert-RuntimeMutationPath -Path $staging -Parent $runtimeParent
        if (Test-Path -LiteralPath $runtime) {
            Move-Item -LiteralPath $runtime -Destination $backup
            $movedOld = $true
        }
        Move-Item -LiteralPath $staging -Destination $runtime
        $movedNew = $true
        if (-not (Test-TkPython $runtimePython)) { throw "自足 runtime/python.exe 复制后 Tk 探针失败" }
        if ($movedOld) {
            Assert-RuntimeMutationPath -Path $backup -Parent $runtimeParent
            Remove-Item -LiteralPath $backup -Recurse -Force
        }
    } catch {
        if ($movedNew -and (Test-Path -LiteralPath $runtime)) {
            Assert-RuntimeMutationPath -Path $runtime -Parent $runtimeParent
            Remove-Item -LiteralPath $runtime -Recurse -Force
        }
        if ($movedOld -and (Test-Path -LiteralPath $backup)) {
            Assert-RuntimeMutationPath -Path $backup -Parent $runtimeParent
            Move-Item -LiteralPath $backup -Destination $runtime
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $staging) {
            Assert-RuntimeMutationPath -Path $staging -Parent $runtimeParent
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
    }
    return $runtimePython
}

function Invoke-OnlineHelper {
    param([string]$Python, [ValidateSet("plan", "install")][string]$Command, [string]$Offline)
    $args = @($HelperPath, $Command, "--root", $Root, "--manifest", $ManifestPath, "--components", ($Components -join ","), "--python", $Python, "--receipt-root", $ReceiptRoot)
    if ($CachePath) { $args += @("--cache", [IO.Path]::GetFullPath($CachePath)) }
    if ($Offline) { $args += @("--offline-package", [IO.Path]::GetFullPath($Offline)) }
    if ($CacheOnly) { $args += "--cache-only" }
    Write-ProgressEvent @{ event = "helper_start"; command = $Command; offline_package = $Offline }
    & $Python -B @args 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-ProgressEvent @{ event = "helper_output"; line = $line }
    }
    $exitCode = $LASTEXITCODE
    Write-ProgressEvent @{ event = "helper_exit"; command = $Command; exit_code = $exitCode }
    if ($exitCode -ne 0) { throw "组件引导失败，退出码 $exitCode；请查看 $ProgressPath" }
}

function Write-StaticPlan {
    param([object]$Manifest)
    # Planning is also useful on a clean machine before Python exists.  Use
    # only the checked-in catalog/source JSON here; this branch never downloads
    # or mutates the checkout.
    $catalogPath = Join-Path $Root "runtime-manifest\components.json"
    if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) { throw "缺少组件目录：$catalogPath" }
    $catalog = Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $sources = @($Manifest.sources) + @($Manifest.bootstrap_packages)
    $sourceById = @{}
    foreach ($source in $sources) { if ($source.component_id) { $sourceById[[string]$source.component_id] = $source } }
    $componentById = @{}
    foreach ($component in @($catalog.components)) { $componentById[[string]$component.id] = $component }
    $selected = [ordered]@{}
    $queue = New-Object 'System.Collections.Generic.Queue[string]'
    if ($Components -contains "all") {
        foreach ($component in @($catalog.components)) { $queue.Enqueue([string]$component.id) }
        foreach ($source in @($Manifest.bootstrap_packages)) { $queue.Enqueue([string]$source.component_id) }
    } else {
        foreach ($requested in $Components) {
            $id = if ($componentById.ContainsKey($requested)) { $requested } elseif ($componentById.ContainsKey("mode.$requested")) { "mode.$requested" } else { $requested }
            $queue.Enqueue([string]$id)
        }
    }
    while ($queue.Count -gt 0) {
        $id = $queue.Dequeue()
        if ($selected.Contains($id)) { continue }
        if (-not $componentById.ContainsKey($id)) {
            if ($sourceById.ContainsKey($id)) { $selected[$id] = $true }
            continue
        }
        $selected[$id] = $true
        foreach ($dependency in @($componentById[$id].dependencies)) { if ($dependency) { $queue.Enqueue([string]$dependency) } }
        if ($sourceById.ContainsKey($id)) {
            foreach ($dependency in @($sourceById[$id].bootstrap_dependencies)) { if ($dependency) { $queue.Enqueue([string]$dependency) } }
        }
    }
    if ($Components -contains "polish") {
        foreach ($source in @($Manifest.bootstrap_packages)) { $queue.Enqueue([string]$source.component_id) }
        while ($queue.Count -gt 0) { $id = $queue.Dequeue(); if ($sourceById.ContainsKey($id)) { $selected[$id] = $true } }
    }
    $rows = @()
    foreach ($id in $selected.Keys) {
        $source = if ($sourceById.ContainsKey($id)) { $sourceById[$id] } else { $null }
        $rows += [ordered]@{
            component_id = $id
            status = if ($source) { [string]$source.status } else { "UNAVAILABLE" }
            version = if ($source) { $source.version } else { $null }
            install_strategy = if ($source) { $source.install_strategy } else { $null }
            reason = if ($source) { $source.reason } else { "没有已核验的来源记录" }
            network = "not_started"
        }
    }
    [ordered]@{
        schema_version = 1
        status = "PLAN_ONLY"
        python_required = $false
        network = "not_started"
        requested = @($Components)
        component_ids = @($selected.Keys)
        rows = $rows
        note = "静态计划未启动 Python、下载或安装；安装请用 -InstallPython 后执行，不会把计划当成安装成功。"
    } | ConvertTo-Json -Depth 12
}

try {
    $manifest = Get-SourceManifest
    if (-not (Test-Path -LiteralPath $VersionPath -PathType Leaf)) { throw "缺少 VERSION：$VersionPath" }
    if ($manifest.app_version -ne (Get-Content -LiteralPath $VersionPath -Raw -Encoding UTF8).Trim()) { throw "来源清单与 VERSION 不匹配" }
    if ($List) {
        $manifest.sources | Select-Object component_id,status,version,kind,install_strategy,reason | Format-Table -Wrap
        if ($manifest.bootstrap_packages) {
            Write-Output "`nPolish bootstrap packages:"
            $manifest.bootstrap_packages | Select-Object component_id,status,version,kind,install_strategy,reason | Format-Table -Wrap
        }
        exit 0
    }
    if ($Plan) {
        $python = Find-TkPython
        if ($python) { Invoke-OnlineHelper -Python $python -Command plan }
        else { Write-StaticPlan -Manifest $manifest }
        exit 0
    }
    if (($Components -join ',') -match '^(editors|tool\.(audacity|lmms|musescore|neuralnote))(,tool\.(audacity|lmms|musescore|neuralnote))*$') {
        $python = Ensure-BootstrapPython
        $editorSources = Get-Content -LiteralPath (Join-Path $Root 'packaging\editor-sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $editorNames = if (($Components -join ',') -eq 'editors') { @('audacity','lmms','musescore','neuralnote') } else { @(($Components -join ',').Split(',') | ForEach-Object { $_.Substring(5) }) }
        $editorCache = Join-Path $UserDataRoot 'component-cache'
        foreach ($name in $editorNames) {
            $entry = $editorSources.editors.$name
            if ($entry.url -notmatch '^https://github\.com/' -or $entry.filename -match '[\\/:]') { throw '编辑器来源或文件名无效' }
            $destination = Join-Path $editorCache $entry.filename
            if (-not (Test-Path -LiteralPath $destination -PathType Leaf) -or
                (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
                Get-VerifiedFile -Url $entry.url -Destination $destination -ExpectedSha256 $entry.sha256 -ExpectedBytes $entry.size | Out-Null
            }
        }
        $editorHelper = Join-Path $Root 'core\editor_install.py'
        $editorArgs = @('-B', $editorHelper, '--root', $Root, '--data-root', $UserDataRoot, '--tools', ($Components -join ','))
        if ($CacheOnly) { $editorArgs += '--cache-only' }
        & $python @editorArgs
        if ($LASTEXITCODE -ne 0) { throw '外部编辑器安装失败，原有编辑器设置保留' }
        Write-ProgressEvent @{ event='installer_complete'; status='SUCCESS'; scope='external-editors' }
        exit 0
    }
    $python = Ensure-BootstrapPython
    $runtimePython = Ensure-RuntimeLayout -Python $python
    if ($OfflinePackage.Count -gt 0) {
        foreach ($package in $OfflinePackage) {
            if (-not (Test-Path -LiteralPath $package)) { throw "离线包不存在：$package" }
            Invoke-OnlineHelper -Python $runtimePython -Command install -Offline $package
        }
    } else {
        Invoke-OnlineHelper -Python $runtimePython -Command install
    }
    Write-ProgressEvent @{ event = "installer_complete"; status = "SUCCESS"; root = $Root }
    exit 0
} catch {
    Write-ProgressEvent @{ event = "installer_failed"; status = "FAILED"; error = $_.Exception.Message }
    exit 1
}
