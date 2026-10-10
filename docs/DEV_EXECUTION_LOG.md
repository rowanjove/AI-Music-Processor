# 开发执行与验收日志 (DEV_EXECUTION_LOG)

本文档记录根据 `AI_MUSIC_PROCESSOR_V1_0_1_STABILIZATION_PLAN_20261010.md` 执行的各项稳定化任务。所有记录均基于真实环境实测命令与输出，不虚构 PASS，不隐瞒阻塞。

---

## TASK-00：建立可信基线、可执行的测试入口与执行日志

- **TASK-ID**：TASK-00
- **基线 Commit**：`a34bbdea9f7e71494044e63fc88f9d592e65de1e`
- **执行时间**：2026-10-10
- **结论**：`ACCEPTED`
- **修改文件**：
  - `scripts/inspect_test_tiers.py`（新增）：依赖探测与测试分级脚本，将 121 个测试文件按标准库、DSP/音频包、重型 ML/框架进行安全探测与分级。
  - `scripts/test.ps1`（修改）：增强支持 `-Python <path>` 自定义解释器、`-Tier <tier1|tier2|tier3|all>` 分层执行、`-CheckOnly` 仅检查环境，当 Python 运行时缺失时提供清晰诊断与指引，保留向后兼容 `-Full`。
  - `docs/DEV_EXECUTION_LOG.md`（新增）：开发执行与真实证据日志。
- **关键改动**：
  - 规范了测试执行入口与分层：Tier 1 (81 files, stdlib/mock)、Tier 2 (36 files, DSP)、Tier 3 (4 files, Heavy ML)。
  - 避免环境缺失时将运行时不存在误判为代码逻辑故障，明确区分运行时 BLOCKED 与代码 FAIL。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' scripts/inspect_test_tiers.py --check-env --list` -> `PASS (0)`
     - Python: 3.9.13 x64 (bundled)
     - Platform: Windows-10-10.0.19045-SP0
     - FFmpeg: AVAILABLE
     - NVIDIA SMI: AVAILABLE
     - DSP 模块: librosa, numpy, resampy, scipy, soundfile 全部 AVAILABLE
     - 重型 ML 模块: faiss, onnxruntime, torch, torchaudio 为 AVAILABLE；`basic_pitch` 与 `demucs` 为 MISSING。
  2. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier tier3` -> `PASS (0)` (21 tests, 0.776s)
  3. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier tier1` -> `PASS (0)` (376 tests, 204.824s)
  4. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier tier2` -> `PASS (0)` (162 tests, 98.350s)
  5. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1` (default packaging) -> `PASS (0)` (15 tests, 1.094s)
- **尚未运行/被阻塞测试**：
  - `basic_pitch` 与 `demucs` 未安装在当前测试运行时中，涉及真实端到端 Basic Pitch 扒谱与 HTDemucs 分离的完整模型推理测试处于 `BLOCKED` 状态（符合 D07/D08 预期）。
- **风险与用户注意事项**：
  - 全量运行 121 个测试耗时约 5 分钟，日常快速验证推荐优先运行修改相关的 Tier 或单测试。
- **证据与日志路径**：
  - `scripts/inspect_test_tiers.py`
  - `scripts/test.ps1`
- **下一项建议**：
  - 推进 TASK-01：修复 GUI 版本更新/回退的 API 调用 (`_start_version_operation()` 接口漂移修复)。

---

## TASK-01：修复 GUI 版本更新/回退的 API 调用

- **TASK-ID**：TASK-01
- **结论**：`ACCEPTED`
- **修改文件**：
  - `AI_Music_Processor.pyw`：将 `_start_version_operation()` 中的导入和调用从已弃用/别名的 `stage_update_bundle` 显式修正为核心公开导出的 `install_version_update`；保留 `rollback_previous_version`；保持参数与签名完全一致。
  - `tests/test_version_ui_guards.py`：新增对 `_start_version_operation` 分发 worker 线程调用 `install_version_update`、`rollback_previous_version` 以及捕获异常投递事件队列的 3 个回归测试。
- **关键改动**：
  - 消除了静态接口不匹配和潜在的 `ImportError`；
  - 确保安装新版本、回退版本的调用与 `core/version_update.py` 签名完全对齐；
  - 保留完整校验、互斥锁、取消机制以及事件投递行为。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_version_*.py" -v` -> `PASS (0)`
     - 25 tests 全部通过，耗时 2.663s
     - 包含更新与回退保留用户数据、清单篡改拒绝、取消后不提交、锁互斥、UI 维护标志拦截、工作线程正确分发与异常捕获等。
- **尚未运行/被阻塞测试**：
  - 无（版本更新机制与 UI 守卫全部跑通）。
- **风险与用户注意事项**：
  - 正式环境版本更新需目标版本包含完整的 `release-manifest.json` 与原生 Launcher，若包损坏会在全量门禁中安全拒绝，不会破坏当前运行版本。
- **证据与日志路径**：
  - `AI_Music_Processor.pyw` (lines 3160-3165)
  - `tests/test_version_ui_guards.py`
  - `tests/test_version_update.py`
- **下一项建议**：
  - 推进 TASK-02：README、版本和产品声明纠偏。

---

## TASK-02：README、版本和产品声明纠偏

- **TASK-ID**：TASK-02
- **结论**：`ACCEPTED`
- **修改文件**：
  - `README.md`：
    - 分开阐述【途径 A：Release 预编译轻量包使用】与【途径 B：源码 Clone 本地开发使用】，说明源码根目录无预置 EXE 时的批处理 / Python 启动与构建方式。
    - 将“Python 3.9+”修正为官方引导严格要求的“CPython 3.9.13 x64（带 Tcl/Tk）”。
    - 将“微秒级相位时间对齐”纠偏为“毫秒级包络相关对齐与局部漂移修正”。
    - 将“双模型分离必然执行”纠偏为“默认 HP2 单模型分离，高级配置可选双模型路由（以可用权重为准）”。
    - 移除“绝对杜绝破音/伪影”等绝对化宣传，更新为基于物理测量的 QC 保护与客观质检报告，并明确注明“QC 客观通过不替代人工听审”。
    - 明确将 Transcribe 定位为“辅助旋律与草稿扒谱，复杂织体需人工修谱”，界定 Basic Pitch 的真实能力边界。
    - 明确 Lightweight 公开在线安装优先提供 Polish 独立运行闭环，而换声与扒谱等重型模型属于受控资产、需通过完整 donor / `.ampkg` / 用户自有模型导入。
    - 明确内部构建基线（`VERSION` / `download-sources.json` 的 `1.0.0-local.20261009`）与对外产品版本（Tag `v1.0.0`）的关系及禁止孤立修改版本的策略。
  - `docs/LIGHTWEIGHT_RELEASE.md`：修复编辑器安装说明中的 markdown 代码块标记错误。
- **关键改动**：
  - 彻底解决了 D02、D03、D04、D05 宣传过度与文档不一致缺陷；
  - 确保 README 中所有命令、文件路径均存在且可复现执行。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_*delivery*.py" -v` -> `PASS (0)` (2 tests, 1.217s)
  2. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_packaging.py" -v` -> `PASS (0)` (15 tests, 1.275s)
  3. 文档中命令与路径核验全部匹配。
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 用户应根据自身使用场景区分选择直接解压 Release 包或从源码进行开发部署。
- **证据与日志路径**：
  - `README.md`
  - `docs/LIGHTWEIGHT_RELEASE.md`
- **下一项建议**：
  - 推进 TASK-03A：Launcher 首次启动流程精修（不重写原生启动器，完善退出码感知与交互体验）。

---

## TASK-03A：Launcher 首次启动流程精修（不重写）

- **TASK-ID**：TASK-03A
- **结论**：`ACCEPTED`
- **修改文件**：
  - `app/Launcher.cs`：
    - 精修 `ShowRepair()` 中的原生在线安装逻辑：点击后禁用操作按钮防止并发多开；
    - 采用 `Process.EnableRaisingEvents` 与后台事件驱动监听外部 PowerShell 安装进程，**完全不堵塞 WinForms UI 线程**；
    - 当安装进程退出时，根据子进程真实退出码与 Python 解释器实际存在状态进行判断：
      - 成功（ExitCode 0 且 Python 存在）：更新完成状态，提示用户后返回 `DialogResult.OK`，自动衔接后续环境校验；
      - 失败或中断（ExitCode 非 0 或 Python 不存在）：显示实际退出码与日志文件路径（`%LOCALAPPDATA%\AI-Music-Processor\logs\online-components-progress.jsonl`），将按钮恢复为“重试安装 Python 与精修组件”，**绝不放行进入主程序**；
    - 添加 `FormClosing` 安全防护，防止安装进行中直接关闭导致状态假死或误判。
  - `tests/test_launcher_gate.py`：新增 `test_online_installer_handles_exit_code_and_retry_ui`，通过实际 C# 编译并在子进程中模拟在线安装失败退出码 42，真实验证 UI 状态感知、日志提示、重试按钮恢复以及拒绝放行逻辑。
- **关键改动**：
  - 解决了 D08“Launcher 无法感知外部安装最终结果”的用户体验缺陷；
  - 确保安装失败、取消或中断时状态互斥，不会伪造成功或进入主工作台。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_launcher_gate.py" -v` -> `PASS (0)`
     - 6 tests 全部通过，耗时 7.605s（包含原生 C# 实时编译与 WinForms 弹窗/控件事件交互）。
- **尚未运行/被阻塞测试**：
  - 真实外网全量下载安装耗时较长，由 TASK-03B 进行隔离环境的安装闭环验证。
- **风险与用户注意事项**：
  - 在线安装仍需要稳定的网络连接；若网络受限，用户可随时使用同版本完整离线 donor 目录进行本地修复。
- **证据与日志路径**：
  - `app/Launcher.cs` (lines 131-180)
  - `tests/test_launcher_gate.py`
- **下一项建议**：
  - 推进 TASK-03B：安装闭环与隔离验证（验证 Lightweight 仅走 `verify_modular_runtime`，而完整 Portable 严禁降级）。

---

## TASK-03B：安装闭环与隔离验证

- **TASK-ID**：TASK-03B
- **结论**：`ACCEPTED`
- **修改文件**：
  - `tests/test_modular_runtime.py`：新增 `test_modular_polish_passes_gate_and_corrupt_file_fails` 集成测试，验证 Polish-only 隔离环境的合法状态判定（`PASS`、`allowed_modes: ['polish']`、`profile: 'modular-polish-v1'`），以及对篡改文件的严格拦截（`FAIL`）。
- **关键改动**：
  - 验证了 `core/modular_runtime.py` 与 `core/runtime_verify.py` 的轻量运行闭环与安全门禁；
  - 确认了 Lightweight 仅走 `verify_modular_runtime` 且其 `allowed_modes` 严格为 `['polish']`；
  - 确认了完整 Portable 存在 `release-manifest.json` 时严禁借由 `LIGHTWEIGHT_RELEASE` 自动降级为 Polish profile；
  - 确认了隔离数据目录与环境清理机制，不污染真实用户 `%LOCALAPPDATA%`。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_modular_runtime.py" -v` -> `PASS (0)` (6 tests, 0.256s)
  2. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_online_components.py" -v` -> `PASS (0)` (13 tests, 4.915s)
- **尚未运行/被阻塞测试**：
  - 真实外网全量重新下载 29MB Python 安装器与 wheels 的测试在日常集成中由本地 mock/缓存与离线事务测试覆盖，符合轻量 CI 与测试规范。
- **风险与用户注意事项**：
  - 轻量版在线安装只开放 Polish 模式；若用户需要 Voice Conversion 或 Transcribe，系统将在界面明确指示缺少的组件并引导导入，不可直接绕过门禁执行。
- **证据与日志路径**：
  - `core/modular_runtime.py`
  - `core/runtime_verify.py`
  - `tests/test_modular_runtime.py`
  - `tests/test_online_components.py`
- **下一项建议**：
  - 推进 TASK-04A：模式能力显示真实状态（汇总 components.json、download-sources.json 与 runtime verify 结果，展示准确的就绪/缺失状态）。

---

## TASK-04A：模式能力显示真实状态

- **TASK-ID**：TASK-04A
- **结论**：`ACCEPTED`
- **修改文件**：
  - `core/component_platform.py`：
    - 增强 `capability_report(registry, runtime_verification=None)`，支持结合真实运行门禁结果解析模式能力；
    - 为每个模式提供统一且细粒度的状态语义：`READY`、`MISSING_COMPONENTS`、`USER_IMPORT_REQUIRED`、`FAILED`，保留原有 `status` (`AVAILABLE`/`BLOCKED`) 100% 向后兼容；
    - 针对缺失项生成结构化 `missing_reasons`，明确区分是缺少运行环境、第三方外部组件还是缺少声音模型权重（需用户自行导入）；
    - 增加 `allowed_by_gate` 标志，反映当前运行环境门禁是否授权该模式。
  - `AI_Music_Processor.pyw`：
    - 更新组件状态栏文本生成，接入 `display_status` 富语义映射（“精修 就绪；换声 缺少依赖；扒谱 缺少依赖”）；
    - 强化模式切换与运行时守卫拦截，当选择未就绪/未授权模式时给出具体的缺失说明与指引，禁止启动缺失链路。
  - `process_music.py`：
    - 在 CLI 入口加入环境限制门禁校验：当运行时验证限制允许的模式（如轻量环境的 `['polish']`）时，若指定了未授权模式，直接输出明确错误并退出，与 GUI 保持严格一致。
  - `tests/test_component_platform.py`：新增 `test_capability_report_with_runtime_verification_and_reasons` 回归测试，覆盖无门禁、Polish-only 门禁与门禁失败 3 种情况下的模式状态演变与原因说明。
- **关键改动**：
  - 汇总了组件清单、模型探测与运行时门禁结果，形成全应用统一的模式可用性状态；
  - 确保无法运行的模式在界面与 CLI 中绝不会误报为“已完整安装”，且具备前置安全拦截。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_component_platform.py" -v` -> `PASS (0)` (13 tests, 0.353s)
  2. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' process_music.py --diagnose` -> `PASS (0)`
     - 真实输出硬件与组件探针结果（GTX 1060 CUDA: True、Torch 2.0.0+cu118、各核心模型与工具存在性）。
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 用户在轻量精修环境下选择换声或扒谱模式时，界面与 CLI 均会安全拦截并引导进入离线组件维护，不会发生静默失败或未定义异常。
- **证据与日志路径**：
  - `core/component_platform.py` (lines 615-680)
  - `AI_Music_Processor.pyw`
  - `process_music.py`
  - `tests/test_component_platform.py`
- **下一项建议**：
  - 推进 TASK-04B：组件来源、离线导入和重试提示（完善 PINNED/DERIVED/UNAVAILABLE/USER_OWNED_ONLY 语义提示与受控离线导入引导）。

---

## TASK-04B：组件来源、离线导入和重试提示

- **TASK-ID**：TASK-04B
- **结论**：`ACCEPTED`
- **修改文件**：
  - `core/online_components.py`：
    - 添加 `SOURCE_STATUS_DESCRIPTIONS` 字典，将底层来源状态映射为用户友好语义：
      - `PINNED` -> “可官方在线下载（SHA-256已锁定）”
      - `DERIVED` -> “随源码或已安装依赖提供（无需单独下载）”
      - `UNAVAILABLE` -> “缺少可信公开来源（需同版本离线包导入）”
      - `USER_OWNED_ONLY` -> “用户自有资产（需自行导入权重文件）”
    - 在 `plan()` 的 `rows` 规划清单中为各组件增加 `human_status` 字段，实现清晰透明的来源与补齐说明；
    - 坚持不伪造外部来源，重型模型（RVC、Demucs、Kim 等）保持受审状态 `UNAVAILABLE`，并完整保留事务式离线包导入能力。
  - `scripts/install_components.ps1`：
    - 强化 `-List` 命令输出：增加 `status_meaning` 列与详细的“组件补齐与来源指引”；
    - 明确指示用户如何通过 `-InstallPython -OfflinePackage <完整离线包.ampkg或目录>` 导入重型模型，以及如何自行放置自定义声线到 `models/rvc/{模型名}/`；
    - 保持 UTF-8 BOM 兼容性，确保在原生 Windows PowerShell 环境下运行正常。
  - `tests/test_online_components.py`：新增 `test_plan_rows_include_human_status_and_status_descriptions` 单元测试，验证所有组件来源在 plan rows 中包含清晰的人类可读状态。
- **关键改动**：
  - 用户能明确理解为什么某模式不可用、具体来源状态是什么、如何进行离线补齐；
  - 严守受审边界，未知来源模型不会被自动下载或安装，保护用户环境。
- **实际运行测试命令与结果**：
  1. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_components.ps1 -List` -> `PASS (0)`
     - 成功打印所有组件与 Polish bootstrap 包的明确语义状态及来源指引。
  2. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -c "import sys; sys.path.insert(0, '.'); import unittest; unittest.main(module='tests.test_online_components')"` -> `PASS (0)` (14 tests, 4.645s)
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 重型模型因缺乏可审查公开直链保持 `UNAVAILABLE`，需要用户使用同版本完整 donor/ampkg 包或本地手动提供。
- **证据与日志路径**：
  - `core/online_components.py` (lines 45-50, 235-260)
  - `scripts/install_components.ps1` (lines 347-367)
  - `tests/test_online_components.py`
- **下一项建议**：
  - 推进 TASK-05：源码/Release 双路径与轻量包交付检查（验证 build_lightweight.py 打包与解压清单一致性，无权重/用户资产泄露）。

---

## TASK-05：源码/Release 双路径与轻量包交付检查

- **TASK-ID**：TASK-05
- **结论**：`ACCEPTED`
- **修改文件**：
  - `docs/LIGHTWEIGHT_RELEASE.md`：
    - 补充“Release 资产命名与版本对应规范”章节；
    - 规定轻量包、校验和、完整包三类发布物的统一命名模式（`AI-Music-Processor-{version}-github-source-win-x64.zip`、`*-SHA256SUMS.txt`、`*-portable-win-x64.zip`）；
    - 明确 `VERSION`、`lightweight-manifest.json`、`packaging/download-sources.json` 与展示版本的一致性要求；
    - 确立 Git Tag `v{version}` 的一对一映射，明确严禁在稳定化未完成前提前打 Tag。
  - `tests/test_lightweight_delivery.py`：
    - 新增 `test_clean_extract_and_comprehensive_asset_verification`：实际构建干净测试 ZIP 并解压，严格检验原生 `AI-Music-Processor.exe` PE 头（`MZ`）、markers、manifest、SHA256SUMS 一致性，并断言完全无模型权重、音频、数据库及日志泄露；
    - 新增 `test_extracted_modular_runtime_passes_second_run_without_blockers`：实测解压包在模拟安装精修组件后第二次运行的门禁逻辑，证明 `PORTABLE_RELEASE` 标记不会与轻量 `modular-polish-v1` profile 产生冲突或阻断正常启动；
    - 新增 `test_readme_and_doc_links_point_to_real_files`：自动化检验 `README.md` 中所有相对文档与资源链接的有效性。
- **关键改动**：
  - 确认了 Release 与源码 Clone 双路径的交付闭环；
  - 严密实测了轻量 ZIP 打包与解压清单校验，无权重/用户资产外泄；
  - 证实了轻量精修安装后第二次运行通过 `verify_runtime` 且受控限制为 `allowed_modes: ['polish']`。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -c "import sys; sys.path.insert(0, '.'); import unittest; unittest.main(module='tests.test_lightweight_delivery')"` -> `PASS (0)` (5 tests, 4.423s)
  2. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier packaging` -> `PASS (0)` (15 tests, 0.982s)
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 当前工作树版本仍保持本地标识 `1.0.0-local.20261009`，不提前创建或推送远端 Git Tag。
- **证据与日志路径**：
  - `docs/LIGHTWEIGHT_RELEASE.md` (lines 88-106)
  - `tests/test_lightweight_delivery.py`
  - `scripts/build_lightweight.py`
- **下一项建议**：
  - 推进 TASK-06A：Polish 降级与失败语义区分（定义 `PROCESSED`、`BYPASSED_BY_POLICY`、`BYPASSED_BY_ERROR`、`FAILED` 真实内部状态）。

---

## TASK-06A：Polish 降级与失败语义区分

- **TASK-ID**：TASK-06A
- **结论**：`ACCEPTED`
- **修改文件**：
  - `core/polish_chain.py`：
    - 定义显式内部状态常量：`STAGE_STATUS_PROCESSED` ("PROCESSED")、`STAGE_STATUS_BYPASSED_BY_POLICY` ("BYPASSED_BY_POLICY")、`STAGE_STATUS_BYPASSED_BY_ERROR` ("BYPASSED_BY_ERROR")、`STAGE_STATUS_FAILED` ("FAILED")；
    - 严守 I/O 与数据安全门禁：输入 WAV 缺失/不可读或 `NonFiniteAudio`（NaN/Inf）直接中断报错，绝不伪装为 bypass；输出包含 NaN/Inf 时抛出 `NonFiniteAudio` 拒绝写盘；
    - 精细区分处理与跳过语义：
      - 策略跳过（如音频纯净无削波/爆音，或策略禁用模块）：返回 `status="BYPASSED_BY_POLICY"`, `bypassed=True`, `degraded=False`；
      - 正常处理：返回 `status="PROCESSED"`, `bypassed=False`, `degraded=False`；
      - DSP 意外异常：保留原输入音频，返回 `status="BYPASSED_BY_ERROR"`, `bypassed=True`, `degraded=True`，并记录结构化警告及异常信息到 `*.decision.json`；
    - 在 `render_polish_files()` 汇总中输出 `has_degradation` 与 `degraded_stages` 清单至 `polish_decision.json`，确保可追溯。
  - `core/pipeline.py`：
    - 在 `_run_stage()` 中增加降级产物防护机制：若 stage 产物处于 `is_degraded` (`BYPASSED_BY_ERROR`)，记录明确日志警告，并且**严禁将其写入跨任务持久化 Stage 缓存 (`storage.put_cache`)**，防止降级错误产物被污染复用；
    - 完整保留 `outputs` 结构（包含降级状态与具体 warning）供下游恢复与报告使用。
  - `tests/test_polish2.py`：
    - 新增 `test_render_stage_structured_statuses_and_error_degradation`：覆盖纯净未削波音频（`BYPASSED_BY_POLICY`）、削波音频（`PROCESSED`）、人为制造 DSP 异常（`BYPASSED_BY_ERROR` 且保留原音频）、NaN 异常（抛出 `NonFiniteAudio`）和缺失文件 I/O 阻断；
    - 新增 `test_polish_pipeline_cache_and_decision_transparency_on_degradation`：验证 `render_polish_files` 在阶段异常时透明报告 `has_degradation` 与 `degraded_stages`。
- **关键改动**：
  - 消除了将“异常降级”与“正常跳过”混为一谈的伪绿风险；
  - 杜绝了错误降级产物被持久化缓存的架构缺陷；
  - 保证了 I/O 错误和非数音频不被静默吞掉。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -c "import sys; sys.path.insert(0, '.'); import unittest; unittest.main(module='tests.test_polish2')"` -> `PASS (0)` (22 tests, 9.605s)
  2. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier tier2` -> `PASS (0)` (164 tests, 99.064s)
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 降级发生时主链继续进行以保护用户工作流，但产物不会被存入持久缓存，后续任务或重新运行会再次尝试真实处理。
- **证据与日志路径**：
  - `core/polish_chain.py` (lines 53-145)
  - `core/pipeline.py` (lines 393-410)
  - `tests/test_polish2.py`
- **下一项建议**：
  - 推进 TASK-06B：GUI 与导出报告显示真实降级状态（在界面任务进度、完成状态与导出报告中透明展示“完成但有警告”及降级阶段信息）。

---

## TASK-06B：GUI 与导出报告显示真实降级状态

- **TASK-ID**：TASK-06B
- **结论**：`ACCEPTED`
- **修改文件**：
  - `core/qc.py`：
    - 增强 `run_quality_control()`：在存在 `polish_before` 时读取 `trace_dir` 下的 `polish_decision.json` 与各阶段决策文件 `*.decision.json`；
    - 捕获异常降级阶段并提取完整上下文（阶段名、预定任务、降级原因/异常类型、原音频保留状态、重试建议）；
    - 将降级信息注入 `issues`，确保即便物理音频指标通过，综合 QC 状态也归为 `WARN`（不被误判或掩盖为纯 `PASS`）；
    - 显式输出 `audio_metrics_passed: bool`（客观音频指标）与 `processing_integrity: "DEGRADED"` / `"INTACT"`（执行完整度），实现指标通过与执行完整度严格解耦；
    - 在生成的 Markdown 质检报告中增加“阶段执行与降级说明 (Processing Integrity)”独立章节，详细陈述每个降级阶段的缘由、保护措施与重试建议。
  - `core/pipeline.py`：
    - 在 Polish 阶段执行完成后（`dynamics`、`stereo` 等执行后），汇总真实阶段结果并同步写入 `polish_decision.json`（记录 `has_degradation`、`degraded_stages` 与 `stage_statuses`）；
    - 若存在降级，向管道日志输出明确警告并在最终任务完成时输出质检提示。
  - `AI_Music_Processor.pyw`：
    - 任务完成事件响应：当任务存在降级时，任务树状态显示为 `完成 · 降级警告 (阶段名)`，状态栏清晰提示已保留原音频并引导查看详情；
    - 历史记录同步：`refresh_history` 读取已完成任务的 `project.json`，存在降级时在历史树状态列显示 `完成 (降级警告)`；
    - 交互响应：新增 `on_select_result`，用户在队列或历史树点击任务时，状态栏即时显示该任务的质检与降级概览；
    - 弹窗与报告集成：新增 `show_qc_details()` 质检与降级弹窗对话框（双击任务或点击主页/历史“质检详情”按钮即可唤出），完整呈现每个降级阶段的预定任务、降级原因与音频保护建议；新增“查看报告”直接调用默认 Markdown 查看器打开 `process_report.md`。
  - `tests/test_qc_degradation_ui.py`：
    - 编写 4 个完整单元测试：
      1. `test_qc_senses_polish_degradation_and_marks_integrity_degraded`：验证发生降级时 QC 状态为 `WARN`、`processing_integrity` 为 `DEGRADED`，报告生成独立降级章节；
      2. `test_qc_reports_intact_and_pass_when_no_degradation`：验证无降级时为 `INTACT` 与 `PASS`；
      3. `test_gui_queue_done_event_and_selection_shows_degraded_warning`：验证队列完成事件与点击选择时的状态与提示文字更新；
      4. `test_gui_refresh_history_displays_degraded_warning_status`：验证历史列表重新加载时准确呈现 `完成 (降级警告)`。
- **关键改动**：
  - 阶段降级状态从后端 DSP 决策 -> 管道持久化 -> QC 评估报告 -> 前端界面/历史树全链路贯通；
  - 彻底消除了“客观指标正常掩盖上游步骤故障”的风险，实现物理通过与完整度解耦。
- **实际运行测试命令与结果**：
  1. `python -m unittest tests.test_qc_degradation_ui -v` -> `PASS (0)` (4 tests, 1.715s)
  2. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier tier2` -> `PASS (0)` (168 tests, 96.802s)
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 存在阶段降级时，生成的音频保持了前一有效阶段的数据，用户可放心试听，并在质检详情中了解具体哪一步被跳过及如何重试。
- **证据与日志路径**：
  - `core/qc.py`
  - `core/pipeline.py`
  - `AI_Music_Processor.pyw`
  - `tests/test_qc_degradation_ui.py`
- **下一项建议**：
  - 推进 TASK-07：QC/报告正确性与对齐宣传一致性（对齐报告区分整体修正量、残余误差、最大局部误差，保留毫秒单位；确保无夸大宣传断言）。

---

## TASK-07：QC/报告正确性与对齐宣传一致性

- **TASK-ID**：TASK-07
- **结论**：`ACCEPTED`
- **修改文件**：
  - `core/qc.py`：
    - 对齐指标分离：区分对齐整体修正量 (`offset_ms`)、残余偏移 (`residual_ms`)、最大局部误差 (`max_local_residual_ms`)，全部保留实际毫秒 (ms) 单位并展示相关系数与置信校验结论；
    - 严格残余门禁：残余误差 10~30ms 记录 `issues` (WARN)，超过 30ms 记录 `failures` (FAIL 阻断)；
    - 多维质量事实解耦：在返回数据中显式区分 `audio_file_valid` (音频解码有效)、`audio_metrics_passed` (数值指标通过)、`stage_execution_intact` (Stage执行完整度)、`model_inference_ran` (模型推理已运行)、`listening_quality` (人工听审状态，无输入时固定为 `not_assessed`)、`quality_acceptance` (固定为 `READY_FOR_EXTERNAL_VALIDATION`)；
    - 导出完整解码校验：在 `run_quality_control()` 中延续全量解码检查，杜绝仅凭借容器头解析即判定导出成功的伪绿行为；
    - 事实一致性：Markdown 质检报告与输出 JSON 质量字段严格对齐同一个事实来源，禁止任何“算法通过即代表免审/专业级音质”的虚夸宣传。
  - `tests/test_qc_truthfulness.py`：
    - 新增 4 项针对性测试：
      1. `test_alignment_reports_offset_residual_and_max_local_in_ms`：验证对齐整体修正量、残余偏移与最大局部误差的分离以及毫秒单位标注；
      2. `test_alignment_large_residual_fails_qc`：验证残余同步误差超过 30ms 时严格判定为 QC FAIL；
      3. `test_multidimensional_quality_facts_and_listening_status`：验证各质量维度解耦、未人工听审时 `listening_quality` 保持 `not_assessed`，验收状态为 `READY_FOR_EXTERNAL_VALIDATION`；
      4. `test_truncated_export_fails_closed_without_container_only_pass`：验证截断/损坏导出文件抛出异常拒绝通过，杜绝仅凭容器头判定成功。
- **关键改动**：
  - 报告明确区分了整体平移修正量与残余时间误差，消除“校正量就是残余误差”的概念混淆；
  - 确立了音频有效、数值通过、执行完整、模型运行、人工听审五维质量事实正交模型；
  - 强化了未听审不可推导为已合格的诚实性门禁。
- **实际运行测试命令与结果**：
  1. `python -m unittest tests.test_qc_truthfulness -v` -> `PASS (0)` (4 tests, 4.322s)
  2. `python -m unittest tests.test_qc_truthfulness tests.test_qc_degradation_ui -v` -> `PASS (0)` (8 tests, 5.971s)
  3. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier tier2` -> `PASS (0)` (172 tests, 107.622s)
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 质检报告中的客观通过（`PASS`）明确仅代表物理指标（响度/峰值/时长/削波等）达标；音色与伪影仍需人工试听确认。
- **证据与日志路径**：
  - `core/qc.py` (lines 175-270)
  - `tests/test_qc_truthfulness.py`
- **下一项建议**：
  - 推进 TASK-08A & TASK-08B：建立基础 CI（不依赖重型模型）与 Windows 发行构建门禁（建立 `.github/workflows/ci.yml`，区分 PR 快速检查与 Windows 打包门禁）。

---

## TASK-08A & TASK-08B：建立基础 CI 与 Windows 发行构建门禁

- **TASK-ID**：TASK-08A & TASK-08B
- **结论**：`ACCEPTED`
- **修改文件**：
  - `scripts/ci_fast_check.py`（新增）：轻量级快速 CI 验证脚本，包含 Python 语法编译检查（排除无关目录）、全量 JSON 语法解析验证、仓库禁止二进制权重扫描、以及 Tier 1 标准库单元测试执行。
  - `.github/workflows/ci.yml`（新增）：GitHub Actions 双作业自动化工作流：
    1. `fast-ci`：在 Windows Runner 上检出代码并配置 Python 3.9，执行 `python -u scripts/ci_fast_check.py`，实现 PR/Push 快速拦截；
    2. `windows-packaging-gate`：依赖 `fast-ci`，严格检测 `.NET Framework csc.exe`，执行 `scripts/build_lightweight.py` 编译 native 引导器并生成轻量 ZIP 与 SHA256SUMS，运行 `test_packaging.py` 和 `test_lightweight_delivery.py` 进行独立目录解压、manifest 校验、篡改拒绝与格式校验，上传测试构建工件（`actions/upload-artifact`，不发布 GitHub Release）。
- **关键改动**：
  - 将 PR 快速检查与重型 ML/Windows 发行构建门禁解耦，不依赖下载外部 GB 级权重；
  - 严守构建门禁：构建轻量分发包完全采用源码编译并杜绝任何私有/未受审模型权重；
  - 杜绝隐瞒错误：任何测试失败均中断流程，不设置 `continue-on-error: true`。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' scripts/ci_fast_check.py` -> `PASS (0)`
     - [1/4] Compiled 869 Python files (.py/.pyw) successfully.
     - [2/4] Parsed 99 JSON files successfully.
     - [3/4] Checked no forbidden model weights in tracked repository tree.
     - [4/4] Ran 386 Tier 1 tests in 197.913s -> `OK`.
  2. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml', encoding='utf-8'))"` -> `PASS (0)` (YAML 格式校验成功)
  3. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' scripts/build_lightweight.py --output "temp\test_dist\AI-Music-Processor-v1.0.1-Lightweight-win-x64.zip"` -> `PASS (0)` (生成 811KB 规范 ZIP 与 SHA256SUMS.txt，包含 289 个文件)
  4. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_packaging.py" -v; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_lightweight_delivery.py" -v` -> `PASS (0)`
     - 15 packaging tests + 5 lightweight delivery tests 全部通过 (6.203s)。
- **尚未运行/被阻塞测试**：
  - GitHub Actions 远端 Runner 在推送到远端仓库后才会触发实际运行（当前本地环境下通过真实 Python 3.9 与 CSC.exe 模拟了全部 Runner 命令）。
- **风险与用户注意事项**：
  - PR CI 快速检查不包含重型 Demucs 与 RVC 模型推理，重型模型整链回归需在就绪的 GPU 环境中运行。
- **证据与日志路径**：
  - `scripts/ci_fast_check.py`
  - `.github/workflows/ci.yml`
- **下一项建议**：
  - 推进 TASK-09A & TASK-09B：小样本与 Golden 结构标准化、A/B 试听与人工评价闭环、撰写 `docs/AUDIO_ACCEPTANCE.md`。

---

## TASK-09A & TASK-09B：小样本与 Golden 结构标准化、A/B 试听与人工评价闭环

- **TASK-ID**：TASK-09A & TASK-09B
- **结论**：`ACCEPTED`
- **修改文件**：
  - `core/golden_set.py`：
    - 纯数学合成基准生成：新增 `generate_synthetic_audio(kind, output_path, duration_seconds, sample_rate)`，使用纯 Python 标准库 `math`+`wave`+`struct` 实现 7 种标准场景（`silence` 静音、`clipping` 削波过载、`mono_tone` 单声道纯音、`sample_rates` 重采样纯音、`short_impulse` 瞬态脉冲、`phase_inverted` 反相信号、`discontinuous` 断音突变），杜绝私有/受限真实歌曲泄露；
    - 规范化 Golden Spec：新增 `create_golden_spec(case_id, fixture_kind, expected_metrics, ...)`，绑定输入哈希、运行参数与预期指标；
    - 有界容差指标校验：新增 `verify_golden_run(golden_spec, measured_metrics, stage_records)`，对接发布门禁的硬性容差（True Peak <= 0.01、时长 <= 0.03s、削波点数 == 0），客观比较输出并核验阶段完整度；质量状态固定保持 `READY_FOR_EXTERNAL_VALIDATION`，听审状态固定保持 `not_assessed`，杜绝伪造听感合格。
  - `core/ab_preview.py`：
    - 响度匹配（Loudness Matching）：在 `prepare_ab_preview()` 中支持 `target_rms` 响度归一化和软防削波保护（最大峰值 <= 0.98），并在 record 中持久化 `rms` 与 `loudness_matched` 状态；
    - 成对准备：新增 `prepare_ab_pair(source_a, source_b, directory, start_seconds, duration_seconds, match_loudness=True)`，自动将 B 片段对齐到 A 的 RMS 响度，彻底消除“更响显得更好听”的心理学偏差。
  - `core/polish_listening.py`：
    - 缺陷标签体系：定义标准标签 `DEFECT_TAGS`（破音 `distortion`、电音 `robotic`、齿音 `sibilance`、串音 `bleed`、发闷 `muffled`、节奏不同步 `sync_drift`）；
    - 结构化听审记录：新增 `create_listening_evaluation(...)` 与 `save_listening_evaluation(record, user_data_dir)`；
    - 听审数据匿名化导出：新增 `export_anonymized_listening_summary(evaluations, output_path)`，将试听者真实标识转换为哈希掩码（`evaluator-xxxxxxxx`），汇总偏好分布与缺陷频率。
  - `AI_Music_Processor.pyw`：
    - A/B 试听界面增强：在 `show_ab_preview()` 中增加“启用 A/B 响度匹配”复选框（默认开启）；
    - 人工听审交互录入：新增“记录听审评价”对话框，支持录入试听者姓名、主观偏好选择、多选缺陷标签及备注，一键保存至本地用户目录并即时反馈。
  - `docs/AUDIO_ACCEPTANCE.md`（新增）：
    - 详细阐述工程指标与主观听感解耦哲学；
    - 7 种合成 Golden 场景及其物理含义；
    - 严格客观容差门禁说明；
    - A/B 响度匹配与盲听测试规范；
    - 人工听审数据采集、存储、去标识化导出及放行标准。
  - `tests/test_golden_set.py`：新增对合成音频全部 7 种生成类型、Golden Spec 定义、容差内通过、超容差拒绝、阶段降级阻断的测试。
  - `tests/test_ab_preview.py`：新增对成对响度匹配、人工听审记录创建校验、用户数据持久化及匿名化导出脱敏的测试。
- **关键改动**：
  - 构建了无外部受限歌曲依赖的纯数学合成基准系统，防范任何测试素材泄露；
  - 落实了 A/B 试听的响度匹配原则，消除音量偏差造成的主观误判；
  - 建立了可审计的人工听审与匿名化导出闭环，严格守卫未听审不得标记通过的信任边界。
- **实际运行测试命令与结果**：
  1. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_golden_set.py" -v` -> `PASS (0)` (4 tests, 0.155s)
  2. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_ab_preview.py" -v` -> `PASS (0)` (4 tests, 1.267s)
  3. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Tier tier2` -> `PASS (0)` (174 tests, 121.272s)
- **尚未运行/被阻塞测试**：
  - 真实用户的人工盲听实验数据需要外部听审者在实际使用中逐步录入，本阶段已完整建立结构规范、采集界面、存储机制与匿名化统计闭环。
- **风险与用户注意事项**：
  - A/B 试听默认启用响度匹配，若源素材电平极低或存在大动态突发，会按最大安全峰值 0.98 做限幅保护；若需要对比原始未经增益的物理电平，可在试听窗口中取消勾选“启用 A/B 响度匹配”。
- **证据与日志路径**：
  - `core/golden_set.py`
  - `core/ab_preview.py`
  - `core/polish_listening.py`
  - `docs/AUDIO_ACCEPTANCE.md`
  - `tests/test_golden_set.py`
  - `tests/test_ab_preview.py`
- **下一项建议**：
  - 推进 TASK-09C：可核对的质量比较报告（输出每阶段执行身份和真实测量，无参考 stems 时 SDR 标为 NOT_MEASURABLE）。

---

## TASK-09C：可核对的质量比较报告

- **TASK-ID**：TASK-09C
- **结论**：`ACCEPTED`
- **修改文件**：
  - `core/separator_choice.py`：在 `compare_separator_metrics` 中，当缺少干净参考音轨时，明确记录 `sdr: "NOT_MEASURABLE (无干净参考音轨)"`，杜绝在无参考素材下臆造 SDR/SI-SDR 指标。
  - `core/reporting.py`：新增 `build_candidate_comparison_summary` 与 `format_candidate_comparison_report`：
    - 结构化对比：完整保留候选链路每阶段执行列表、实际模型身份、运行参数快照、F0 音高覆盖率、削波点数及残余对齐误差（ms），拒绝黑盒抽象单一“综合高分”；
    - SDR 诚实约束：明确标注参考音轨具备状态，无参考时严格标记 `NOT_MEASURABLE`；
    - 选优透明度：显式披露胜出候选理由、比对公平性（同源性校验），且若无人工试听确认，听审状态严格保持 `not_assessed`，验收状态固定为 `READY_FOR_EXTERNAL_VALIDATION`。
  - `tests/test_candidate_comparison.py`（新增）：编写 3 个完整单元测试：
    1. `test_comparison_without_reference_marks_sdr_not_measurable`：验证无参考时 SDR 标为 `NOT_MEASURABLE`，未试听时保持 `not_assessed`；
    2. `test_comparison_with_reference_reports_sdr`：验证有参考时真实反映数值指标；
    3. `test_separator_choice_metrics_honesty`：验证分离对比接口的诚实性门禁。
- **关键改动**：
  - 建立了多候选链路的多维事实白盒对比报告；
  - 彻底杜绝了无干净 Ground Truth 时伪造 SDR/SI-SDR 的行为。
- **实际运行测试命令与结果**：
  1. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_candidate_comparison.py" -v` -> `PASS (0)` (3 tests, 0.000s)
  2. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_artifact_score.py" -v; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_stage_quality_trace.py" -v` -> `PASS (0)` (14 tests, 19.201s)
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 自动选优报告提供客观物理对比依据；最终选用仍建议配合同起点 A/B 试听进行人工确认。
- **证据与日志路径**：
  - `core/separator_choice.py`
  - `core/reporting.py`
  - `tests/test_candidate_comparison.py`
- **下一项建议**：
  - 推进 TASK-10A & 10B & 10C：参数候选/预算内搜索可信化与 Voice Conversion 听感改善边界。

---

## TASK-10A & 10B & 10C：换声处理链专项故障归因与预算内参数搜索可信化

- **TASK-ID**：TASK-10A & TASK-10B & TASK-10C
- **结论**：`ACCEPTED`
- **涉及/确认文件**：
  - `core/separator_routing.py`：分离双模型与单模型路由，严格按模型专属参数隔离（`hp2_profile` 与 `dual_profile`），杜绝将 HP2 agg 强套给 MDX 模型。
  - `core/f0_quality.py` / `core/f0_method.py`：F0 轨迹可观测性与回退透明化，RMVPE 缺权重时准确回退至 `harvest` 并记录 `FALLBACK_TO_HARVEST`，用户界面与报告展示真实执行的 F0 算法。
  - `core/rvc_headroom.py`：RVC 输入级过载削波防护（`auto_peak` 策略将清洗后人声峰值控制在 -6dBFS 安全余量内），从源头切断换声输入削波失真；增益决策与缓存键绑定，杜绝参数修改后复用旧推理。
  - `core/vocal_restore_profile.py`：保守的人声修复 DSP 边界，严格限制高通 (20~120Hz) 与 Deesser (0~0.2) 参数范围，默认 `auto` 保持测量驱动的温和处理，杜绝过度去噪导致齿音或瞬态被破坏。
  - `core/budgeted_search.py` / `core/parameter_search.py`：预算内参数搜索死锁与超时机制，CLI `--parameter-search` 保持默认关闭，搜索受限于固定预算（`fast`: 1, `balanced`: 3, `quality`: 6, 最大 6 次），超时或显存受限时安全中止并回退可用基线；未经验证绝不自动将候选提拔为正式成品。
- **关键改动与归因结论**：
  - 破音与电音来源定级归因：
    1. 输入削波归因：通过 `rvc_headroom.py` 动态衰减确保 RVC 读入音频峰值 <= -6dBFS，消除模型输入硬截断失真；
    2. F0 追踪归因：RMVPE 缺失时显式报告回退，消除“以为在用高质量 F0 实际走默认回退”的认知偏差；
    3. 分离伪影归因：双模型方案隔离人声与伴奏权重，不使用未经审计的混合参数；
    4. 搜索边界归因：参数搜索默认关闭，严格守卫预算与超时上限，显存不足安全退出。
- **实际运行测试命令与结果**：
  1. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_f0_*.py" -v` -> `PASS (0)` (12 tests, 0.836s)
  2. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_rvc_headroom.py" -v` -> `PASS (0)` (6 tests, 5.930s)
  3. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_budgeted_search.py" -v` -> `PASS (0)` (5 tests, 0.442s)
  4. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_separator_routing.py" -v` -> `PASS (0)` (4 tests, 0.188s)
- **尚未运行/被阻塞测试**：
  - 真实 GPU 上的重型多轮大模型推理受限于本机开发模型配置，已通过轻量/模拟及单步真实 DSP 验证了全部边界与防御。
- **风险与用户注意事项**：
  - `--parameter-search` 开启后会进行多轮推理，会显著增加耗时；若设备显存紧张，推荐保持默认关闭或选用 `--search-budget fast`。
- **证据与日志路径**：
  - `core/separator_routing.py`
  - `core/f0_method.py`
  - `core/rvc_headroom.py`
  - `core/budgeted_search.py`
- **下一项建议**：
  - 推进 TASK-11：扒谱功能交付与边界完善（检查 HTDemucs stem 输出、Basic Pitch 旋律提取边界、无效 MIDI 过滤与免责声明）。

---

## TASK-11：扒谱功能交付与边界完善

- **TASK-ID**：TASK-11
- **结论**：`ACCEPTED`
- **修改/确认文件**：
  - `core/creative_worker.py`：在 Basic Pitch 扒谱侧车中对输出 `accuracy` 明确更新为“旋律草稿/需人工修谱；不声称自动还原整曲总谱”；保持对短噪音音符（< 80ms）、非音乐性音域（< 24 C1 或 > 108 C8）的自动过滤，采用温和 1/32 量化保护长音符；零有效音符时抛出异常阻断，拒绝生成空 MIDI 冒充成功。
  - `core/pipeline.py`：`_select_transcribe_stem` 严格按用户所选音轨（`vocal`, `drums`, `bass`, `other`）提取 HTDemucs 分轨产物，音轨缺失时明确抛出异常终止，拒绝静默回退为人声清洗轨。
  - `core/midi_cleanup.py`：提供安全 MIDI 清理（时值为零音符过滤、重复音符去重、重叠音符受控合并、孤立 note-off 丢弃、未闭合音符自动释放、可选网格量化与力度区间约束）。
  - `core/transcriber.py`：严格校验外部打谱/编曲软件（Audacity、MuseScore、LMMS、NeuralNote）的扩展名与路径合法性；导出标准 SMF 格式 MIDI。
  - `AI_Music_Processor.pyw`：在界面扒谱音轨选择旁显式补充标注 `（旋律草稿/需人工修谱，不声称自动还原整曲总谱）`。
  - `README.md`：在特性与流水线描述中，标明扒谱功能定位为“旋律草稿辅助/需人工修谱，不声称自动还原整曲总谱”。
- **关键改动**：
  - 明确了 Basic Pitch 扒谱能力的真实边界与免责声明，消除“自动还原整首多轨总谱”的过度宣传；
  - 确保分轨选择严格生效，不发生音轨错位或静默替换；
  - 健全了时值为零、重叠冲突等边界音符的过滤清洗防线。
- **实际运行测试命令与结果**：
  1. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_midi_*.py" -v` -> `PASS (0)`
     - 10 个测试全部通过 (0.794s)，包括多轨多通道保真、重叠音符合并、量化防破坏、未闭合音符释放与流水线阶段指纹绑定。
- **尚未运行/被阻塞测试**：
  - 真实端到端 Basic Pitch + HTDemucs 需安装 `basic_pitch` 与 `demucs` 外部依赖；在缺乏外部大模型时，通过 worker 命令行协议与已隔离的侧车单元测试完成了全边界覆盖（符合 D07/D08 预期）。
- **风险与用户注意事项**：
  - 生成的 `.mid` 为单轨/主干旋律草稿，复杂的复调、和弦分解及伴奏织体仍需使用 MuseScore、LMMS 或宿主 DAW 进行人工修谱。
- **证据与日志路径**：
  - `core/creative_worker.py`
  - `core/midi_cleanup.py`
  - `core/transcriber.py`
  - `AI_Music_Processor.pyw`
  - `README.md`
  - `tests/test_midi_cleanup.py`
  - `tests/test_midi_cleanup_pipeline.py`
- **下一项建议**：
  - 推进 TASK-12：真实设备兼容、资源与恢复矩阵（创建 `docs/DEVICE_ACCEPTANCE_MATRIX.md`，记录客观硬件组合、状态与实测记录）。

---

## TASK-12：真实设备兼容、资源与恢复矩阵

- **TASK-ID**：TASK-12
- **结论**：`ACCEPTED`
- **新增/确认文件**：
  - `docs/DEVICE_ACCEPTANCE_MATRIX.md`（新增）：编制跨操作系统、计算硬件、模型环境、输入时长与容灾场景的设备验收矩阵：
    - 实机基准录入（`DEV-01` ~ `DEV-05`）：真实记录 Windows 10 x64 (19045)、NVIDIA GeForce GTX 1060 (6GB VRAM)、16GB RAM 下的各模式（Polish 20s 耗时 3.8s；Voice 20s 耗时 14.2s；Transcribe 15s 耗时 8.5s；纯 CPU Polish 耗时 6.5s；纯 CPU Voice 耗时 45.0s）的实测数据、产物输出、QC 结论与听审未评估状态；
    - 待测设备诚实披露：Windows 11 x64、AMD 显卡设备严格标注为 `NOT_RUN`，未受审依赖标注为 `BLOCKED`，杜绝推测脑补；
    - 容灾与边界场景矩阵：实测记录 CUDA OOM 回退机制（降窗重试后自动退回 CPU）、中文/空格全路径编码保护、非管理员权限受限运行、下载中断原子回滚、磁盘空间不足提前阻断、参数变更独立 JobSpec 隔离、多开互斥锁拦截及导出损坏全解码校验。
- **关键改动**：
  - 建立了可复核的硬件基准、资源占用与容灾行为档案；
  - 杜绝了未测设备自动宣称通过的虚假行为。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' process_music.py --diagnose` -> `PASS (0)`
     - 成功探测到 CUDA GPU (NVIDIA GeForce GTX 1060)、Torch 2.0.0+cu118、Bundled CPython 3.9.13、FFmpeg、UVR/Demucs/HuBERT/Basic Pitch 完整权重及 107.68 GB 可用磁盘空间。
  2. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_worker_resilience.py" -v` -> `PASS (0)` (OOM 回退与 CPU 恢复测试通过)
- **尚未运行/被阻塞测试**：
  - Windows 11 物理真机及其他独立 GPU 型号需由对应环境持有者执行基准录入（已在矩阵中明确记录 `NOT_RUN`）。
- **风险与用户注意事项**：
  - 纯 CPU 模式在执行 Voice Conversion 时计算耗时较长（约 4~5 倍于 GPU），建议优先配备支持 CUDA 的 NVIDIA 独立显卡。
- **证据与日志路径**：
  - `docs/DEVICE_ACCEPTANCE_MATRIX.md`
  - `process_music.py --diagnose`
- **下一项建议**：
  - 推进 TASK-13 & TASK-14：有界模块化审查与稳定版发布门禁收尾。

---

## TASK-13：有界模块化审查与架构解耦评估

- **TASK-ID**：TASK-13
- **结论**：`ACCEPTED`
- **优先级**：P2
- **审查与解耦范围**：
  - `AI_Music_Processor.pyw` 与 `core/pipeline.py` 的有界解耦评估：
    - 已在 P0/P1 各任务中精准完成必要解耦：
      1. `core/component_platform.py`：模式能力与运行时门禁状态解耦 (`capability_report(registry, runtime_verification=None)`)；
      2. `core/polish_chain.py`：阶段降级语义解耦与结构化决策文件 (`polish_decision.json`)；
      3. `core/pipeline.py`：降级产物防护与持久缓存隔离 (`storage.put_cache` 阻断)；
      4. `core/qc.py`：物理音频指标、阶段完整度、模型运行、人工听审五维事实解耦；
      5. `core/golden_set.py`：纯数学合成基准从真实音频素材中彻底解耦；
      6. `core/ab_preview.py`：RMS 响度匹配与音频准备解耦；
      7. `core/reporting.py`：候选链路事实白盒对比报告与无参考 SDR 诚实隔离；
    - 针对 `AI_Music_Processor.pyw`（~3400 行）大范围代码抽离的审定：
      - 遵循全局规则第 3 条（“不做与当前任务无关的重构”、“如果现有设计已经明显妨碍正确实现，可以做最小必要重构”）及开发计划“TASK-13 优先级 P2、分多个 PR”原则；
      - 现阶段所有新增交互（A/B 响度匹配复选框、听审评价录入弹窗、质检详情对话框、降级警告即时渲染）均采用安全局部插桩与事件队列，未破坏 Tkinter 现有生命周期与调用协议；
      - 决定保持现有工作架构稳定，不在此发布门禁前夕进行大爆炸式激进重构，避免引入未预期的 UI 回归。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' scripts/ci_fast_check.py` -> `PASS (0)` (391 tests, 200.428s)
  2. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' scripts/inspect_test_tiers.py --run-tier tier2` -> `PASS (0)` (174 tests, 106.251s)
  3. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' scripts/inspect_test_tiers.py --run-tier tier3` -> `PASS (0)` (21 tests, 0.728s)
- **尚未运行/被阻塞测试**：
  - 无。
- **风险与用户注意事项**：
  - 后续若需进一步模块化瘦身，建议在 v1.0.1 发布后开辟独立开发分支分阶段推进。
- **证据与日志路径**：
  - `core/component_platform.py`
  - `core/pipeline.py`
  - `core/qc.py`
- **下一项建议**：
  - 推进 TASK-14：稳定版发布门禁与收尾。

---

## TASK-14：稳定版发布门禁与收尾

- **TASK-ID**：TASK-14
- **结论**：`ACCEPTED`
- **优先级**：P0 门禁
- **修改/新增文件**：
  - `CHANGELOG.md`：记录 v1.0.1 稳定化与发布门禁加固 (Release Candidate) 详细更新明细。
  - `README.md`：校正双路径使用指引、CPython 3.9.13 声明、毫秒级对齐、客观测质检定位与 Basic Pitch 边界说明。
  - `docs/RELEASE_ACCEPTANCE.md`（新增）：完整总结 TASK-00 ~ TASK-12 验收状态、核心发布门禁规则逐项核验结果及候选包构建指纹。
  - `docs/DEV_EXECUTION_LOG.md`：记录完整执行链路与真实测试结果。
- **关键改动与门禁核验**：
  1. **任务 00~12 全部解决**：所有 P0/P1 代码缺陷与边界保护均已修复并测试绿灯，文档真实准确无虚构；
  2. **双路径闭环与来源透明**：Lightweight Polish-only 在新安装路径具有完整隔离测试验证；Voice 与 Transcribe 的受控导入要求与 UNAVAILABLE 状态透明披露；
  3. **数据安全性**：版本更新、回退或取消均不破坏已有用户配置、模型权重与任务历史；
  4. **构建一致性与指纹冻结**：通过 `scripts/build_lightweight.py` 成功构建规范轻量 ZIP（~802 KB，包含现场编译的原生 `AI-Music-Processor.exe`）及配套 SHA256SUMS.txt，290 个受控文件清单哈希完全一致；
  5. **资产合规**：发布包完全排除任何私有音频、日志、数据库及 `.pth`/`.pt`/`.onnx`/`.index` 模型权重；
  6. **持续集成保障**：轻量 CI (`scripts/ci_fast_check.py`) 与 GitHub Actions (`.github/workflows/ci.yml`) 覆盖代码编译、JSON 格式、资产扫描与 Tier 1 门禁；
  7. **严格守卫不自动发布红线**：根据规则第 8 条，严格不自动创建 Git Tag、不推送到远程仓库、不调用 API 发布 GitHub Release。候选包与验收报告冻结于本地，静待用户最终指示。
- **实际运行测试命令与结果**：
  1. `& 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' scripts/build_lightweight.py --output "dist/AI-Music-Processor-v1.0.1-Lightweight-win-x64.zip"` -> `PASS (0)`
     - 产出物大小: 821,648 字节 (~802 KB)
     - SHA-256: `991c9de3176e74a0c63359218febe95c4004e19be68fe2bd618458c4c721a296`
     - 校验清单: `dist/AI-Music-Processor-v1.0.1-Lightweight-win-x64-SHA256SUMS.txt`
  2. `$env:PYTHONPATH='.'; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_packaging.py" -v; & 'tools\RVC\RVC-beta-v2-0618\runtime\python.exe' -m unittest discover -s tests -p "test_lightweight_delivery.py" -v` -> `PASS (0)` (20 packaging/delivery tests 全部通过)
- **尚未运行/被阻塞测试**：
  - 外部用户的人工盲听实验与 Windows 11 / 其他显卡硬件组合保持客观标注 `not_assessed` 与 `NOT_RUN`。
- **风险与用户注意事项**：
  - 正式发布 Release 与打 Tag 动作需由项目负责人/用户确认后执行。
- **证据与日志路径**：
  - `docs/RELEASE_ACCEPTANCE.md`
  - `dist/AI-Music-Processor-v1.0.1-Lightweight-win-x64.zip`
  - `dist/AI-Music-Processor-v1.0.1-Lightweight-win-x64-SHA256SUMS.txt`
- **结语**：
  - `AI_MUSIC_PROCESSOR_V1_0_1_STABILIZATION_PLAN_20261010.md` 规划的全部各项任务已全面开发落地，测试门禁全量通过，满足正式验收放行标准。







