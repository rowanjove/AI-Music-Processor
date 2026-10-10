# AI Music Processor (本地 AI 音乐一体化工作台)

<div align="center">

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20x64-0078D6.svg?logo=windows)](https://github.com/rowanjove/AI-Music-Processor)
[![Python](https://img.shields.io/badge/Python-3.9.13%20x64-3776AB.svg?logo=python)](https://www.python.org/)
[![Release](https://img.shields.io/badge/Release-v1.0.0-success.svg)](https://github.com/rowanjove/AI-Music-Processor/releases)

**面向创作者与音频工程师的本地离线 AI 音乐处理工作台**<br/>
**集成音频精修（Polish）· 人声换声重制（Voice Conversion）· 辅助扒谱（Transcribe）**

[快速上手](#-快速上手) • [核心功能](#-核心功能与流水线) • [轻量分发架构](#-轻量分发与按需安装) • [命令行界面](#-cli-命令行使用) • [许可证](#-开源许可证与致谢)

</div>

---

## 📸 界面预览

### 主工作台 (Main Workbench)
![AI Music Processor 主工作台](docs/images/workbench_main.png)

### 换声重制与高级混音配置 (Voice Conversion & Advanced Audio Settings)
![换声重制与高级混音配置](docs/images/advanced_voice_conversion.png)

### 四轨分轨与智能扒谱模式 (4-Stem Separation & MIDI Transcription)
![四轨分轨与智能扒谱模式](docs/images/transcribe_mode.png)

### 模型库与预设管理 (Models & Presets)
![模型库与预设管理](docs/images/model_presets.png)

---

## 🌟 核心特性

- 🔒 **100% 本地运行与数据隐私**：所有音频计算、神经网络推理及项目缓存均在本地完成，无需上传云端，保障创作版权与数据自主。
- 🎛️ **三大核心流水线一体化**：
  - **音频精修 (Audio Polish)**：音频解包 → 缺陷修复 → 均衡音色塑造 → 多频段动态压缩 → 立体声声场拓宽 → 母带处理（支持 True Peak 保护与 LUFS 响度对齐）。
  - **人声换声重制 (Voice Conversion)**：人声/伴奏分离（默认 HP2 单模型，可选 Kim Vocal 2 / Inst HQ 3 双模型路由）→ 人声降噪去杂 → RMVPE/Harvest 音高轮廓提取 → RVC 神经声码换声 → 毫秒级包络相关对齐与局部漂移修正 → 自动侧链避让（Ducking）与混音母带。
  - **分轨与辅助扒谱 (Transcribe)**：HTDemucs 四轨分离（人声/鼓/贝斯/其他）→ Basic Pitch 深度学习旋律草稿提取 → MIDI 自动量化与清理（旋律草稿/需人工修谱，不声称自动还原整曲总谱）→ DAW 工程协同。
- 📊 **端到端客观质检 (Audio QC)**：自动检测全流音频时长、静音段、DC 偏移、NaN/Inf 异常、削波失真、声道 RMS、相关性、LUFS 及换声残余同步误差；客观指标通过与人工听审状态严格分开追踪（QC 通过不代表免除人工听审）。
- ⚡ **自适应硬件调度**：原生支持 NVIDIA CUDA GPU 加速与 FP32/FP16 推理；遇到显存吃紧自动平滑降窗与 CPU 容灾回退，防止任务崩溃。
- 🔄 **断点续做与 Stage 缓存**：支持项目级粒度恢复，分离与推理产物具备 SHA-256 智能缓存指纹，参数未变秒级复用。
- 🔌 **外部 DAW 与编辑器联动**：支持一键拉起或导入 Audacity、LMMS、MuseScore、NeuralNote，衔接专业后期制作流。

---

## 🚀 快速上手

### 系统环境要求
- **操作系统**：Windows 10 / 11 64-bit
- **Python 运行时**：官方引导固定 CPython 3.9.13 x64（带 Tcl/Tk）
- **处理器**：Intel Core i5 / AMD Ryzen 5 及以上
- **显卡推荐**：NVIDIA GeForce GTX 1060 (6GB) 及以上（支持 CUDA），亦完全支持纯 CPU 模式运行
- **内存**：8 GB RAM 以上（推荐 16 GB 及以上）

### 使用方式（双途径）

#### 途径 A：使用 GitHub Release 发布包（推荐普通用户）
1. 从 [Releases](https://github.com/rowanjove/AI-Music-Processor/releases) 下载最新的轻量发布压缩包并解压至英文或无特殊字符目录。
2. 解压后根目录已内置原生引导程序 `AI-Music-Processor.exe`。
3. 双击 `AI-Music-Processor.exe`：
   - 若系统尚未准备专用 Python 3.9.13 运行环境，引导程序将展示**原生安装与修复页面**；
   - 点击“在线安装精修组件”即可由受控安装脚本自动配置 Python 3.9.13、FFmpeg 与精修引擎；
   - 安装完成后自动通过轻量运行时校验并启动主工作台。

#### 途径 B：从源码使用（面向开发者）
源码仓库不预置二进制 EXE，请按以下步骤操作：

```powershell
# 1. 克隆仓库
git clone https://github.com/rowanjove/AI-Music-Processor.git
cd AI-Music-Processor

# 2. 查看支持的组件清单与计划
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 -List

# 3. 按需安装基础运行环境与音频精修依赖（若本机已有兼容环境可跳过 -InstallPython）
powershell -ExecutionPolicy Bypass -File scripts/install_components.ps1 -InstallPython -Components polish

# 4. 启动应用（以下方式任选其一）：
# 方式 4.1：使用便捷批处理（自动检测内置或环境 Python）
.\启动AI音乐一体化处理器.bat

# 方式 4.2：直接用 Python 启动主工作台
python AI_Music_Processor.pyw

# 方式 4.3：如需编译生成原生 AI-Music-Processor.exe 引导器
python scripts/build_lightweight.py
```

---

## 🛠️ 核心功能与流水线

### 1. 音频精修流水线 (Polish Pipeline)
针对伴奏、翻唱干声或播客录音，执行标准母带修正：
```
输入音频 ➔ 解码校验 ➔ 频谱缺陷修复 ➔ EQ音色平衡 ➔ 动态多段压缩 ➔ 立体声拓宽 ➔ 响度测量 ➔ 24-bit True-Peak 母带 ➔ 质检报告
```
- 输出规格：24-bit WAV (44.1kHz / 48kHz)、高质量 MP3 (320kbps)、无损 FLAC。
- 防过载保护：True Peak 衰减防护与物理削波检测，控制数码过载风险。
- **分发状态**：通过公开安装脚本即可在线闭环部署（`CPython 3.9.13 + FFmpeg + DSP 依赖`）。

### 2. 人声换声流水线 (Voice Conversion Pipeline)
```
整首曲目 ➔ 人声/伴奏分离 (默认 HP2 / 可选双模型) ➔ 人声除噪与杂音清洗 ➔ 音高轮廓提取 (Harvest/RMVPE) ➔ RVC 声学特征替换 ➔ 毫秒级包络相位对齐 ➔ 智能避让(Ducking) ➔ 混音母带
```
- 自定义声音模型：支持将合规授权的 RVC 权重放置于 `models/rvc/{模型名称}/model.pth` 与检索索引 `.index`，系统自动识别加载。
- **分发状态**：因模型权重体积与分发权利限制，重型模型未随公开轻量源包自动捆绑，需通过同版本完整 donor 包、受控 `.ampkg` 归档或用户自有模型导入。

### 3. 分轨与智能扒谱 (Transcribe Pipeline)
```
整曲音频 ➔ HTDemucs 4-Stem 分离 (人声/鼓/贝斯/其他) ➔ Basic Pitch 神经网络 ➔ 旋律与音符草稿检测 ➔ 标准 MIDI 导出 ➔ 外部 DAW 协同修谱
```
- 导出的标准 MIDI 文件可送入 MuseScore 排版或通过 LMMS 加载 SoundFont 进行重编曲。
- **能力边界**：Basic Pitch 定位为旋律与草稿辅助工具，多复调与复杂背景伴奏需人工修谱。

---

## 💻 CLI 命令行使用

除了 GUI 桌面交互，`process_music.py` 支持无头批处理与自动化集成：

```powershell
# 1. 纯音频精修模式
python process_music.py "song/guitar.wav" --mode polish --formats wav,flac

# 2. 换声重制模式 (指定模型、半音升降、生成伴奏与多格式母带)
python process_music.py "song/demo.mp3" --mode voice_conversion --model sunyanzi --pitch 7 --midi --formats wav,mp3,flac

# 3. 恢复中断的任务
python process_music.py --resume <job_id>

# 4. 执行系统诊断与环境检查
python process_music.py --diagnose

# 5. 缓存管理与清理预览
python process_music.py --cache-preview --cache-quota-gb 20 --cache-ttl-days 30
```

---

## 📁 项目目录结构

```
AI-Music-Processor/
├── AI-Music-Processor.exe       # 原生快速启动引导器（Release 包预置，或由 build_lightweight.py 编译）
├── AI_Music_Processor.pyw       # Tkinter 桌面主工作台核心逻辑
├── process_music.py             # CLI 命令行处理入口
├── 启动AI音乐一体化处理器.bat   # Windows 便捷启动批处理
├── app/                         # 桌面引导层与 C# 源码 (Launcher.cs)
├── core/                        # 音频核心引擎、DSP、分离、换声与质检逻辑
│   ├── analyzer.py              # 音频特征分析与音域检测
│   ├── pipeline.py              # 核心任务调度与流水线
│   ├── rvc_engine.py            # RVC 推理桥接
│   ├── separator.py             # UVR / Demucs 伴奏人声分离引擎
│   ├── transcriber.py          # Basic Pitch 智能 MIDI 扒谱
│   └── qc.py                    # 综合音频质检与异常分析
├── packaging/                   # 下载源策略与清单 (download-sources.json)
├── runtime-manifest/            # 运行时组件与验证清单配置
├── scripts/                     # 构建、安装与验证脚本
│   ├── build_lightweight.py     # 轻量包构建器（生成 ZIP 及编译 C# Launcher）
│   ├── install_components.ps1   # PowerShell 组件按需安装器
│   ├── test.ps1                 # 自动化测试入口（支持分层测试）
│   └── inspect_test_tiers.py    # 依赖探测与测试分级工具
├── tests/                       # 全量自动化测试套件 (120+ 测试文件)
├── docs/                        # 架构、技术规范与执行日志
│   ├── DEV_EXECUTION_LOG.md     # 真实执行与验证证据日志
│   └── LIGHTWEIGHT_RELEASE.md   # 轻量分发与按需安装规范
├── LICENSE                      # Apache-2.0 许可证
├── THIRD_PARTY_LICENSES.md      # 第三方依赖与开源协议清单
└── VERSION                      # 内部构建基线版本号标识
```

---

## 📦 轻量分发与版本策略说明

本项目采用**轻量分发设计**：
- 源码仓库与 GitHub Release 仅发布轻量源码与 Native 引导程序（小于 30 MB），详见 [LIGHTWEIGHT_RELEASE.md](docs/LIGHTWEIGHT_RELEASE.md)。
- **版本对应关系**：
  - `VERSION` 文件与 `packaging/download-sources.json` 中的标识（如 `1.0.0-local.20261009`）为**内部构建基线**，用于同版本组件校验与清单哈希绑定；
  - GitHub Tag 与 Release 标题（如 `v1.0.0`）为**对外产品发布版本**；
  - 升级或准备新版本时，必须同步维护清单绑定与哈希校验，禁止孤立修改版本标识。

---

## 📄 开源许可证与致谢

- **主项目许可证**：本项目核心源代码采用 [Apache License 2.0](LICENSE) 开源。
- **第三方开源组件致谢**：本项目使用了来自开源社区的优秀成果，包括 FFmpeg、RVC、Demucs、Basic Pitch、PyTorch、NumPy、SciPy、SoundFile 等。所有第三方库及其许可证详见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
- **免责声明与音频版权**：
  1. 本工具仅供音频处理、声音技术研究与个人创作使用。
  2. 用户在使用换声功能时，应确保拥有目标音频及声学模型的合法使用授权，严禁用于侵犯他人合法权益或任何违反法律法规的用途。
  3. 开发者不对因滥用此工具造成的任何法律后果承担责任。

---

<div align="center">
Made with ❤️ by Rowan Jove & Open Source Contributors
</div>
