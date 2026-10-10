# 更新记录

本文件记录本地工程加固的源码与交付状态，不将尚未构建/验收的目标描述为产品版本。

## 2026-10-09 打包前审计源码

- 本地包版本标识更新为 `1.0.0-local.20261009`，保留所有旧交付目录。新包实际门禁和路径以最终发布就绪报告为准。
- 真实双模型分离、RMVPE、Polish阶段链、三模式进度、受审权重加载、浮点RVC、组件与任务身份、缓存物化/跨库源保护均有工程执行或专项测试。
- 补齐历史软删除与恢复、待处理音频只读预分析、转谱轨选择、两路混音增益、20秒真实整链基准、配置备份恢复、版本完整校验更新/回退、损坏编码一次重导再检。
- 新增真实 `.ampkg` ZIP、组件依赖与所属文件校验、可信本地哈希及显式公钥 RSA 验签；工具页/首次向导接入事务修复，跨进程全局互斥，收据保留而解包 payload 清理。
- 新增集中第三方许可档案、原文和 FFmpeg 原生报告；说明字段、元数据身份、索引正文与原文覆盖均受门禁检查，未知授权不放行公开构建。
- 新流程与旧JobSpec兼容分开验证；音质未做人工听审，均保持 `READY_FOR_EXTERNAL_VALIDATION`。详细证据见 `docs/AUDIT_FIX_EVIDENCE_20261009.md`。

## 本地 Portable 验收 profile

- 增加版本文件、明确 allowlist、嵌入运行时健康门禁与本地用户数据根。
- Portable 包仅用于本机验收；文件哈希清单与 ZIP 不构成数字签名。授权/来源未知资产尚未获公开再分发确认。
- 真实 ZIP、解压验证与合成 polish/QC 结果以本轮 `docs/P0_HARDENING.md` 和隔离 evidence 为准；未运行的设备矩阵仍是 NOT_RUN。

## 2026-10-05 本地验收结果

- 已构建完整目录 ZIP 并完成独立解压、34,927 项文件清单前后核对；artifact source commit `124bef2934865975b8f416cae8c0dc10b40461e1`，ZIP SHA-256 `D4EF2C2F678BD520BAD738426274B0D7479103083A37F7147E8EF7930936D999`。
- runtime strict verify、Launcher smoke、合成 Polish/RVC/Transcribe 均有本机证据；总状态 `PASS_WITH_WARNINGS`，仅隔离首次安装数据根中的数据库为 `N/A`。完整测试 94/94 通过。
- clean Windows 10/11、RTX/CPU-only、resume/cache/cancel 和损坏恢复仍 `NOT_RUN`；未知授权不允许把本地验收包描述为公开分发版本。
- 完整逐项证据位于 `docs/P0_HARDENING.md`、`docs/FRESH_MACHINE_ACCEPTANCE.md` 和本机隔离 `dist/` 记录。文档在构建后追加，ZIP 未重打。

## 未发布（本地工程基线）

- 新建 Git 项目边界规则，跟踪自有源码与文档，排除本机配置、用户音频、处理结果、运行库、vendor、模型、日志、数据库及构建产物。
- 固定文本文件与 Windows 脚本的换行约定。
- 当前应用自报标识仍为 `1.0.0-local`；本条目不代表发布包或版本标签。
