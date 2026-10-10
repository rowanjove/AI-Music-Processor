# 稳定版发布验收报告 (Release Acceptance Report)

本文档记录根据 `AI_MUSIC_PROCESSOR_V1_0_1_STABILIZATION_PLAN_20261010.md` 执行的稳定版发布门禁审查与验收结论。

- **发布候选标识 (Candidate)**：`v1.0.1-rc`
- **目标交付形态**：Lightweight Source-Bootstrap（纯源码原生引导分发包）+ 规范受控组件导入
- **审查日期**：2026-10-10
- **发布授权状态**：**待用户确认（本阶段严格不自动打 Tag、不发布 GitHub Release）**

---

## 1. 任务完成与验证状态汇总 (TASK-00 ~ TASK-12)

| 任务编号 | 任务主题 | 状态 | 关键交付物与验证证据 |
|---|---|---|---|
| **TASK-00** | 测试分级与基线建立 | `ACCEPTED` | `scripts/inspect_test_tiers.py`、`scripts/test.ps1`，Tier 1 (386) / Tier 2 (174) / Tier 3 (21) 全绿 |
| **TASK-01** | GUI 版本更新接口修复 | `ACCEPTED` | 对齐 `install_version_update` 静态导入，`test_version_ui_guards.py` 25 个防呆测试通过 |
| **TASK-02** | 文档事实纠偏与宣传克制 | `ACCEPTED` | README.md / LIGHTWEIGHT_RELEASE.md 修复双路径说明、CPython 3.9.13 x64 与毫秒级对齐 |
| **TASK-03A** | Launcher 原生安装引导精修 | `ACCEPTED` | `app/Launcher.cs` 原生 `ShowRepair()` 异步事件监听、退出码感知、多开互斥与重试防护 |
| **TASK-03B** | 安装闭环与运行时隔离 | `ACCEPTED` | Polish-only 模式隔离，严禁 Portable 自动降级，13 个 online 与 6 个 modular 测试全通过 |
| **TASK-04A** | 模式就绪状态多源归集 | `ACCEPTED` | `core/component_platform.py` 归集 `READY` / `MISSING_COMPONENTS` / `USER_IMPORT_REQUIRED` |
| **TASK-04B** | 组件来源与离线导入透明化 | `ACCEPTED` | `download-sources.json` 状态清晰映射，`install_components.ps1 -OfflinePackage` 指引完善 |
| **TASK-05** | 源码/Release 双路径交付检查 | `ACCEPTED` | `test_lightweight_delivery.py` 验证干净解压、PE 头、manifest、无资产泄露、二次运行无阻断 |
| **TASK-06A** | Polish 降级与失败语义区分 | `ACCEPTED` | `core/polish_chain.py` 细分 `BYPASSED_BY_ERROR` / `FAILED`，拦截 NaN/Inf，阻止降级写入跨任务缓存 |
| **TASK-06B** | GUI 与报告真实降级展示 | `ACCEPTED` | `core/qc.py` 报告降级章节，UI 状态栏/历史树显式展示 `完成 · 降级警告`，弹窗详情与报告快捷唤起 |
| **TASK-07** | QC 真实性与多维解耦 | `ACCEPTED` | 分离整体修正量与残余时间误差（ms），五维事实解耦，未听审固定为 `not_assessed` |
| **TASK-08A** | 基础轻量 CI 检查机制 | `ACCEPTED` | `scripts/ci_fast_check.py` 快速检查语法、JSON、无权重扫描及 386 个 Tier 1 标准库单元测试 |
| **TASK-08B** | Windows 发行构建门禁 | `ACCEPTED` | `.github/workflows/ci.yml` 自动化工作流，检测 `csc.exe`，执行构建，全量 packaging 测试 |
| **TASK-09A** | 合成 Golden 基准标准化 | `ACCEPTED` | `core/golden_set.py` 提供 7 种纯数学合成音频与有界容差比较，杜绝真实音乐资产泄露 |
| **TASK-09B** | A/B 试听与人工听审闭环 | `ACCEPTED` | `core/ab_preview.py` 成对 RMS 响度匹配，缺陷标签多选，本地存储与去标识化匿名导出 |
| **TASK-09C** | 可核对的质量比较报告 | `ACCEPTED` | `core/reporting.py` 输出每阶段白盒参数与实测指标，缺少参考音轨时 SDR 严格标为 `NOT_MEASURABLE` |
| **TASK-10A~C** | 换声归因与预算内搜索 | `ACCEPTED` | RVC 输入 -6dBFS 余量削波保护，RMVPE 缺权重透明回退，参数搜索默认关闭且严格受限于预算与超时 |
| **TASK-11** | 扒谱功能交付与边界完善 | `ACCEPTED` | HTDemucs stem 过滤保护，短音符与音域过滤，UI/文档标明“旋律草稿/需人工修谱，不声称还原总谱” |
| **TASK-12** | 设备兼容与恢复矩阵 | `ACCEPTED` | `docs/DEVICE_ACCEPTANCE_MATRIX.md` 真实记录 GTX 1060 / CPU 实测，未测平台标 `NOT_RUN` |

---

## 2. 核心发布门禁规则逐项核验

### 2.1 资产合规与无私有模型泄露 (Asset Hygiene Gate)
- [x] 轻量分发包完全排除所有 `.pth`、`.pt`、`.onnx`、`.index` 等权重文件；
- [x] 排除了所有用户真实歌曲、历史日志、数据库与临时目录；
- [x] 启动引导 EXE 由 `scripts/build_lightweight.py` 现场调用 Windows 原生 `.NET Framework csc.exe` 从源码 `app/Launcher.cs` 实时编译，杜绝预存未知 PE 二进制文件。

### 2.2 交付文件一致性与校验和 (Checksum Consistency Gate)
- [x] 构建产物输出为包含 `lightweight-manifest.json`、完整源码与脚本的规范 ZIP；
- [x] 自动生成同名 `-SHA256SUMS.txt` 校验和清单，支持标准 sha256 检验工具独立核对；
- [x] 清单内 289 个文件大小与 SHA-256 哈希全部对齐。

### 2.3 诚实性与客观质量声明门禁 (Truthfulness Gate)
- [x] 彻底消除“微秒级相位对齐”、“专业级音质已通过”、“自动还原整首总谱”等过度虚夸宣传；
- [x] 确立了音频有效、数值通过、执行完整、模型运行、人工听审五维质量事实正交模型；
- [x] 未经真实用户试听的流水线与对比报告，`listening_quality` 固定保持 `not_assessed`，验收状态固定保持 `READY_FOR_EXTERNAL_VALIDATION`。

---

## 3. 发布候选包 (Release Candidate) 构建指纹

构建执行命令：
```powershell
python scripts/build_lightweight.py --output "dist/AI-Music-Processor-v1.0.1-Lightweight-win-x64.zip"
```

实测产物度量：
- **目标文件**：`dist/AI-Music-Processor-v1.0.1-Lightweight-win-x64.zip`
- **压缩包大小**：821,648 字节 (~802 KB)
- **SHA-256 哈希**：`991c9de3176e74a0c63359218febe95c4004e19be68fe2bd618458c4c721a296`
- **校验清单**：`dist/AI-Music-Processor-v1.0.1-Lightweight-win-x64-SHA256SUMS.txt`
- **打包文件数**：290 个受控源码与文档文件
- **包含组件**：包含现场编译的 `AI-Music-Processor.exe`（原生 C# WinForms 引导器）、核心 Python 流水线源码、组件安装脚本及说明文档。

---

## 4. 外部放行与发布确认

> [!CAUTION]
> 根据开发规则第 8 条（“不得自动发布”），本系统：
> - **不会自动创建 Git Tag**；
> - **不会推送分支至远端 GitHub 仓库**；
> - **不会调用 API 发布 GitHub Release**。
> 
> 所有本地构建产物、测试日志与验收矩阵已冻结就绪。正式发布操作需由项目负责人/用户明确核准后再行执行。
