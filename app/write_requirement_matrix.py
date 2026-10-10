# -*- coding: utf-8 -*-
import re
from pathlib import Path
root=Path(__file__).resolve().parent.parent
source=root/'AI_Music_Processor_1.0_完整设计方案.md'
text=source.read_text(encoding='utf-8-sig')
entries=re.findall(r'^# (\d+)\. (.+)$',text,re.M)
notes={}
def group(ids,status,note):
    for i in ids: notes[i]=(status,note)
group([1,2,7,110,111,112,116],'参考','产品目标；实际验收以本矩阵及验收报告为准，不承诺主观音质完美。')
group([4,5,60,61,96,97,114,115],'工程取舍','保留 Tk/Python，独立进程 Worker 与现有 RVC runtime/addons；不宣称 Tauri/Rust/React 或 JSONL RPC 已实现。')
group([6,10],'部分','FFmpeg 支持列出的扩展；实测 MP3、WAV、单声道 M4A。加密文件和全部编码排列未验收。')
group([3,83,84],'已验证','不覆盖输入；源 SHA-256 保护、Stage 产物哈希、恢复损坏重跑有实际测试。')
group([8,9,16,98,99,100,101],'部分','四页桌面 UI、实际模型/预设/队列/历史/诊断；高级设置不是原设计全部控件，Tk 构造和 EXE 启动通过，未做完整人工视觉验收。')
group([11],'部分','HP2 两轨及本地 HTDemucs 四轨已实测；没有 MDX/RoFormer 自由路由界面。')
group([12,20,21,22,25],'部分','轻度 HPF/EQ/De-esser、测量增益、人声压缩与轻混响；没有 DeepFilterNet、完整动态 EQ 或独立饱和/总线编辑器。')
group([13,24,66,71,77,79,81,102,103,104,105,106,109],'未实现','见下方明确缺口清单；不以规划中的勾选视为完成。')
group([14,15,17,18],'部分','三模型实测；Profile、自定义目录、FP32、harvest/pm/crepe 与原生能量分段。没有 RMVPE、自动音域分析变调及自动 OOM 降级重试。')
group([19],'已验证','正确双向偏移、可信局部平滑时间修正；真实整曲局部残余最大2.5ms，合成漂移回归通过。')
group([23],'已验证','按实测响度匹配与有界增益，48k 混音；四轨伴奏为 drums+bass+other，未混回 vocals。')
group([26,27,28,29,30,31],'部分','两遍测量母带与各导出 True Peak 修复、真实 QC 阻断；峰值预算可放宽响度。四轨样例仍有响度 WARN；没有通用伪影自动修复。')
group([32,33,34,35,36,37,38,39,63,64,65,67,68,69,73,75,82],'已落地','core/pipeline.py、storage.py、processes.py、gpu.py 与桌面队列；SQLite、唯一 dispatch_id、产物验证、跨项目缓存、串行GPU、失败恢复、参数快照、磁盘预检。相关自动验收见报告。')
group([40],'部分','数据库支持全部 Stage 状态；实际流水线省略无关条件 Stage，没有为所有未启用步骤写 SKIPPED。')
group([41,42],'部分','清理/母带/曲风预设、模型 Profile 与 JSON 导入保存；并非完整复制原 app.json schema 或全部性能档位。')
group([43,44,45],'部分','Basic Pitch 0.4.0 ONNX 主唱 MIDI、音长/频率过滤和轻量网格量化；没有完整重叠音清理、自动歌词、和弦、鼓/贝斯配器或乐谱准确率保证。')
group([46],'受环境限制','MIDI 文件有效，LMMS --import 及独立 SF2 配置已接通；LMMS 与 MuseScore 命令行导入多次超时，编辑器内回放/排版验收未通过。')
group([47,72],'部分','原曲/母带外部试听与 Audacity 真实文件入口；未完成自动化编辑器交互或主观 A/B 听感验收。')
group([48,49,50],'已落地','命令/退出码/耗时/stdout/stderr 日志、Stage 错误及项目报告；超时/取消保留已完成状态。')
group([51],'部分','超时取消、坏输入拒绝、磁盘预检、输出损坏重建；没有完整自动 CPU/OOM/UVR 模型降级重试。')
group([52,53,54,85,86,87],'部分','模型扫描、Profile、SQLite license 记录及 license-manifest.json，默认 Unknown；没有完整授权录入/商用审核 UI。')
group([55,56,57],'已落地','唯一项目名、master/stems/midi/reports、参数/QC/授权清单；不会同秒同名覆盖。')
group([58,59],'部分','GPU 串行、FP32、Demucs6秒窗口及缓存；没有全部 performance 档位。')
group([62],'已落地','本地子进程调用，无需启动 HTTP 服务。')
group([70,108],'部分','真实元信息、全文件 LUFS/TP，前90秒估计 BPM/调性；估计值不可等同准确乐理分析。')
group([74],'部分','JSON 临时文件原子替换、参数校验与队列恢复；没有完整旧 schema 迁移系统。')
group([76,78],'部分','本机 Windows 与 GTX1060 实测；诊断区分可执行文件存在与实际可用，没有其他 Windows 干净设备验收。')
group([80],'部分','三模式真实顺序 Stage 链路、失败续跑；没有通用可视化 DAG 编辑或所有可选节点。')
group([88],'部分','APP_VERSION、参数和工具命令可追溯；没有把所有依赖版本写入每份 Process Report。')
group([89,90,91],'已验证','11项回归、5个真实引擎案例、6项故障恢复检查；证据 JSON 与日志均在本目录。')
group([92,93,113],'部分','自动主链路已贯通；原设计1.0清单仍有下面的缺口，不能标为全面完美验收。')
group([94,95],'参考','范围及开发顺序参考；不是可执行验收证据。')
group([107],'部分','按 Stage 固定百分比更新；不是逐样本进度或准确 ETA。')
lines=['# 需求落地矩阵（2026-10-04）','','逐项对应原设计的116章。已落地表示存在实现；已验证要求实际检查。部分、未实现、受环境限制均不代表验收通过。原设计中的规划勾选不能替代本表。','','| 章 | 原设计要求 | 状态 | 当前实现与边界 |','|---|---|---|---|']
for number,title in entries:
    status,note=notes.get(int(number),('待核对','尚未有完整验收证据。'))
    lines.append(f'| {number} | {title} | {status} | {note} |')
assert len(entries)==116
lines+=['','','## 尚未落地的明确范围','','- 自动音阶 Pitch Correction、自动音域变调、DeepFilterNet、动态伴奏 Ducking。','- 缓存 TTL/容量自动回收、波形预览、首次启动向导、可配置组件路径。','- 所有可选节点 SKIPPED 记录、全局快捷键、Windows Toast、禁止休眠、自动关机、可靠 ETA、自动推荐。','- 完整性能档位、自动 OOM/模型降级、完整授权管理界面、完整依赖版本项目归档。','- Tauri/Rust/React 桌面移植、独立全部组件安装包、跨设备兼容验收。','- MuseScore/LMMS 实际导入回放、完整人工 UI/听感及 MIDI 准确率验收。','','这些不影响已经实际生成的分轨、换声、MIDI 和母带产物，但原文“全部功能完成”条件尚不成立。']
(root/'docs'/'需求落地矩阵.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
notice='> 实施状态更新（2026-10-04）：本文保留原始设计目标，清单勾选不是交付证明。当前结合既有环境采用 Tk/Python 与实际模型，完整自动音频主链路已实测；全部章节的实施、偏离及未完成项见 [需求落地矩阵](docs/需求落地矩阵.md) 和 [验收报告](docs/验收报告.md)。不可将本文理解为所有116章均已实现。\n\n'
if '实施状态更新（2026-10-04）' not in text: source.write_text(notice+text,encoding='utf-8')
