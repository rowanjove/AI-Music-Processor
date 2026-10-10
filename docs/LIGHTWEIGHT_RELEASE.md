# GitHub 轻量分发

源码仓库和 GitHub Release 使用 `scripts/build_lightweight.py` 生成
`github-source-win-x64` 包。该包只包含源码、组件目录、安装策略、测试和
PowerShell 引导器，不包含 `tools/`、`runtime/`、`models/`、音频、数据库、日志、
历史 `dist/` 或用户配置，因此不会把约 8 GB 的完整本机验收包塞进 GitHub。

在源码包根目录打开 PowerShell，可以先查看组件计划：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 -List
```

`-List` 不下载、不启动 Python，也不会修改工作区。要查看某个流程的依赖闭包，
如果机器已有带 Tcl/Tk 的 Python 3.9，安装器会直接用它生成精确计划；没有 Python 时也会输出不下载的静态计划：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 -Components polish -Plan
```

缺少 Python 时可以显式允许引导器下载并运行 Python 官方 3.9.13 x64 安装器：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 -InstallPython -Components polish
```

安装器会验证来源清单中的 HTTPS 主机、文件大小和 SHA-256，验证 Tk 后将 Python
复制到应用约定的 `tools/RVC/RVC-beta-v2-0618/runtime/python.exe`，连同官方安装目录的 DLL、标准库和 Tcl/Tk，避免 Windows venv 的 `Scripts/python.exe` 和绝对 `home=` 路径失效。
每次事件都会追加到用户数据目录 `%LOCALAPPDATA%\AI-Music-Processor\logs\online-components-progress.jsonl`
（也可用 `AI_MUSIC_PROCESSOR_DATA_ROOT` 指定），失败会返回非零退出码并留下
`installer_failed` 记录。收据同样写入该 `logs` 目录，源码目录只接收运行时清单。

`polish` 是可在线形成运行闭包的最小安装目标：引导器会从官方固定来源取得
FFmpeg/ffprobe archive，并将 NumPy、SciPy、SoundFile、cffi、pycparser、mido、packaging 的
固定 CPython 3.9 wheels 暂存后安装到 `tools/RVC/RVC-beta-v2-0618/runtime/Lib/site-packages`，然后逐项执行
导入探针和 FFmpeg `-version` 探针。只有探针通过才会在收据中写 `INSTALLED`，并
生成 `runtime-manifest/bootstrap-runtime.json`。该文件是 `modular-polish-v1`
配置，允许模式只有 `polish`，并逐文件记录 Python/runtime、site-packages、应用源码
和 FFmpeg 的 SHA-256；它不会覆盖完整 donor 使用的 strict `runtime.json`。

RVC vendor、Torch/CUDA GPU 变体、fairseq、分离模型和模型权重必须以同版本完整
donor 或 `.ampkg` 提供；清单提供一个可选的官方 PyPI Torch 2.0.0 CPU wheel 及其
固定依赖，只有显式选择 `runtime.torch` 才会下载。安装器不会把单个 PyPI 归档冒充可运行闭包，也不会自动下载或替用户
再分发名人声音模型：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 `
  -InstallPython -OfflinePackage C:\path\AI-Music-Processor-v1.0.0-local.20261009-win-x64.zip
```

完整 donor 的版本必须与源码 `VERSION` 一致。引导器先暂存并逐文件校验，再写入
应用目录；`config/`、`output/`、`models/`、`song/` 和日志等用户数据目录不能由
donor 覆盖。成功后生成 `runtime-manifest/lightweight-install.json`；真实可运行的
`runtime-manifest/inventory.json`、`runtime.json`、组件注册和模型清单必须来自
完整 donor/ampkg 的已验证内容。只下载到缓存的官方归档会明确返回
`CACHED_NOT_INSTALLED`，不会伪报安装成功。

`packaging/download-sources.json` 将 Python、FFmpeg、Polish wheels 和已具备完整哈希
的 PyPI 归档列为 `PINNED`；RVC、模型及重型 Torch 变体在清单中明确
标为 `UNAVAILABLE` 或 `USER_OWNED_ONLY`，原因写在清单中。补齐来源时必须先经过
版本、哈希、依赖和授权审查。大于 GitHub Release 单资产限制的组件继续使用本地
donor、分片 `.ampkg` 或用户配置的内部缓存。

生成包和校验和：

```powershell
& (Get-Command python).Source -B scripts/build_lightweight.py `
  --output dist/AI-Music-Processor-v1.0.0-local.20261009-github-source-win-x64.zip
```

包内 `lightweight-manifest.json` 对每个源码文件记录大小和 SHA-256；旁边的
`*-SHA256SUMS.txt` 记录 ZIP 本身的 SHA-256。该源包不是完整 Portable 交付物，
音质和 GPU/模型效果仍需在实际运行时完成外部验收。


## 外部编辑器按需安装

工具页选择 tool.audacity / tool.lmms / tool.musescore / tool.neuralnote，再打开安装器。完整包也允许安装外部编辑器；它们放在用户数据目录 external-tools，登记到 config/external-editors.json，不替换应用 Python 或 FFmpeg。

固定来源位于 packaging/editor-sources.json：Audacity 3.7.7、LMMS 1.2.2、MuseScore 4.6.5、NeuralNote 2.0.0。下载必须匹配 SHA-256 和大小。Audacity 使用官方 ZIP；其余使用官方 NSIS、MSI 或 Inno 安装器。安装完成必须找到唯一有效 PE 主程序才登记成功。安装器可能需要 Windows 提权确认；失败返回非零码并保留原编辑器设置。

`powershell
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 -Components tool.audacity
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 -Components editors
`

音频/MIDI 导入和人工听审仍是 READY_FOR_EXTERNAL_VALIDATION；下载安装包不代表这些验收通过。
