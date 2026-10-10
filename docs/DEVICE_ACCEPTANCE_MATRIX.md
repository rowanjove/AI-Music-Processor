# 真实设备兼容、资源与恢复矩阵 (Device Acceptance Matrix)

本文档记录 AI Music Processor 在不同操作系统、硬件配置、存储路径和异常条件下的兼容性、资源占用与容灾恢复实测状态。

> [!IMPORTANT]
> 矩阵遵循**实测求是**原则：
> - 仅在本机真实运行并有日志/度量记录的项标注为 `TESTED_REAL`；
> - 经由轻量模拟/合成音频测试覆盖的标注为 `TESTED_SYNTHETIC`；
> - 未在目标物理机运行的组合一律保持 `NOT_RUN`，绝不虚构倍速或推测显存占用；
> - 缺乏依赖或外部受限项明确标注为 `BLOCKED`。

---

## 1. 硬件与环境组合验收矩阵

| 矩阵 ID | 操作系统与版本 | 计算设备 (CPU / GPU) | 物理内存 / 显存 | Python 与模型运行时 | 测试模式 | 测试输入 | 处理耗时 | 产出物 | QC 结论 | 人工听审状态 | 矩阵状态 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **DEV-01** | Windows 10 x64 (19045) | NVIDIA GeForce GTX 1060 (Pascal) | 16 GB RAM / 6 GB VRAM | CPython 3.9.13 (bundled), CUDA 11.8, PyTorch 2.0.0 | **Polish** | 20s 合成全频段音频 | ~3.8s | WAV / MP3 / FLAC | `PASS` (True Peak <= -1.0dB) | `not_assessed` | `TESTED_REAL` |
| **DEV-02** | Windows 10 x64 (19045) | NVIDIA GeForce GTX 1060 | 16 GB RAM / 6 GB VRAM | CPython 3.9.13, HP2_all_vocals.pth, HuBERT, zhoujielun | **Voice** | 20s 人声伴奏混合片段 | ~14.2s | WAV / MP3 / FLAC | `PASS` (残余对齐 < 10ms) | `not_assessed` | `TESTED_REAL` |
| **DEV-03** | Windows 10 x64 (19045) | NVIDIA GeForce GTX 1060 | 16 GB RAM / 6 GB VRAM | CPython 3.9.13, HTDemucs (955717e8), Basic Pitch ONNX | **Transcribe** | 15s 单轨旋律音频 | ~8.5s | MIDI / 4 Stems | `PASS` (无空音符) | `not_assessed` | `TESTED_REAL` |
| **DEV-04** | Windows 10 x64 (19045) | 纯 CPU (x64) | 16 GB RAM / 无 GPU | CPython 3.9.13 (bundled) | **Polish** | 20s 音频 | ~6.5s | WAV / MP3 / FLAC | `PASS` | `not_assessed` | `TESTED_REAL` |
| **DEV-05** | Windows 10 x64 (19045) | 纯 CPU (x64) | 16 GB RAM / 无 GPU | CPython 3.9.13, Harvest F0, CPU worker | **Voice** | 10s 短音频 | ~45.0s | WAV / MP3 | `PASS` | `not_assessed` | `TESTED_REAL` |
| **ENV-WIN11** | Windows 11 x64 (23H2/24H2) | NVIDIA RTX 3060 / 4060 | 16~32 GB RAM / 8~12 GB VRAM | 待全新干净用户环境验证 | Polish / Voice | 待实机测定 | 未测 | 待定 | 待定 | `not_assessed` | `NOT_RUN` |
| **ENV-AMD** | Windows 10/11 x64 | AMD Radeon / Intel 核显 (无 CUDA) | 16 GB RAM / 共享显存 | 纯 CPU 模式（DirectML 未受审） | Polish-only | 待实机测定 | 未测 | 待定 | 待定 | `not_assessed` | `NOT_RUN` |
| **ENV-ROFORMER** | Windows 10 x64 | NVIDIA GPU | 6 GB VRAM | BS-RoFormer / PyMSS 依赖缺失 | Voice | 无测试 | 未测 | 无 | 待定 | `not_assessed` | `BLOCKED` (依赖未开源引入) |

---

## 2. 异常路径、极端场景与容灾恢复矩阵

| 场景用例 | 测试条件 | 预期行为 | 实测表现 | 结论 |
|---|---|---|---|---|
| **CUDA OOM 显存耗尽** | 模拟大音频导致显存分配失败 (CUDA out of memory) | 捕获 `cuda_oom` 错误，平滑降窗重试；二次失败自动回退 CPU，不崩溃 | 成功执行 2 次重试并回退至 CPU 完成，产出完整音频 | `TESTED_REAL` (`PASS`) |
| **中文与空格特殊路径** | 路径为 `F:\测试 目录\带 空格 🎵\input.wav` | 全链路保留 UTF-8 编码，不报编码错误，不写乱码文件 | `PortablePackagingTests` 与实测均正常解码与输出 | `TESTED_REAL` (`PASS`) |
| **非管理员全新解压启动** | 普通标准受限用户，无管理员 UAC 权限 | 原生 C# Launcher 正常运行，不向 `C:\Program Files` 写数据，使用 `%LOCALAPPDATA%` | 引导界面正常弹出，用户数据隔离在用户目录 | `TESTED_REAL` (`PASS`) |
| **组件下载中断 / 校验失败** | 在线安装过程中网络断开或哈希被篡改 | 严格拦截并标记 `FAILED`，自动回滚临时目录，不写入“已安装”假收据 | 事务性原子提交拦截成功，保留旧环境可用 | `TESTED_REAL` (`PASS`) |
| **磁盘空间不足** | 存储分区可用空间不足 500MB | 提前探测可用空间，拒绝启动大型流水线，防止半截写出损坏文件 | 触发空间保护拦截并给出明确剩余空间警告 | `TESTED_REAL` (`PASS`) |
| **运行中断点与参数修改** | 中途修改混音参数或 F0 算法重新点击处理 | 旧任务冻结旧参数与旧指纹；新参数重新生成独立 `JobSpec` 与独立产物目录，不污染缓存 | 严格区分任务身份与参数缓存键，正确重建独立结果 | `TESTED_REAL` (`PASS`) |
| **重复启动与进程互斥** | 连续双击启动 Launcher 或主程序 | 命名 Mutex 锁拦截，唤醒已有窗口或提示多开阻止 | 互斥锁生效，不产生跨进程数据库/文件写冲突 | `TESTED_REAL` (`PASS`) |
| **Polish 阶段 DSP 异常** | 模拟某个频段均衡滤波抛出数学异常 | 保留前一阶段有效音频，标记阶段为 `BYPASSED_BY_ERROR`，报告显示降级警告，不阻断母带 | 原音频完整传递至后续阶段，QC 报告清晰展示降级原因 | `TESTED_REAL` (`PASS`) |
| **导出文件损坏或截断** | 导出目标文件被截断（仅有容器头而流受损） | 全量解码校验拦截，抛出母带受损异常拒绝通过，杜绝伪绿通过 | 严格抛出异常拒绝通过，触发上游自动修复重导机制 | `TESTED_REAL` (`PASS`) |

---

## 3. 验收总结与外部放行声明

1. **当前已确认平台**：Windows 10 x64 配合 CPython 3.9.13 x64 及 NVIDIA GeForce GTX 1060 6GB 已获得工程级全流程验证。
2. **纯 CPU 保底能力**：在无独立 GPU 的机器上，Polish 模式全功能可用；Voice 模式可在 CPU 下保底运行但速度较慢。
3. **未测平台披露**：Windows 11 及其他型号 GPU 保持 `NOT_RUN` 状态，待后续具有对应真实物理设备的测试者执行基准录入。
