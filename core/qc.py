"""Fail closed: decoded samples, exports and alignment all contribute to QC."""
import datetime
import json
import math
import os
import numpy as np
import soundfile as sf
from core.analyzer import measure_loudness_ebur128, probe_metadata
from core.config import FFMPEG_EXE
from core.processes import run

def correlation_from_moments(cross, energy, dc, total):
    """Pearson correlation from the sums sample_metrics already accumulates."""
    energy = np.asarray(energy, dtype=np.float64)
    dc = np.asarray(dc, dtype=np.float64)
    if total <= 0 or energy.shape[0] < 2 or dc.shape[0] < 2:
        return None
    denom = np.sqrt(np.prod(energy - total * dc * dc))
    if not np.isfinite(denom) or denom <= 1e-12:
        return None
    value = float((cross - total * dc[0] * dc[1]) / denom)
    if not np.isfinite(value):
        return None
    return value


def pearson_correlation(left, right):
    """Full-band correlation through correlation_from_moments. Not a second formula."""
    left = np.asarray(left, dtype=np.float64).reshape(-1)
    right = np.asarray(right, dtype=np.float64).reshape(-1)
    total = int(left.shape[0])
    if total == 0 or right.shape[0] != total:
        return None
    dc = np.array([float(left.mean()), float(right.mean())])
    energy = np.array([float(np.dot(left, left)), float(np.dot(right, right))])
    return correlation_from_moments(float(np.dot(left, right)), energy, dc, total)


def sample_metrics(path):
    total=0
    energy=0.
    peaks=0.
    clipping=0
    channel_sums=None
    channel_energy=None
    cross=0.
    with sf.SoundFile(path) as audio:
        channels=audio.channels
        channel_sums=np.zeros(channels)
        channel_energy=np.zeros(channels)
        while True:
            data=audio.read(65536,dtype='float32',always_2d=True)
            if not len(data):
                break
            if not np.isfinite(data).all():
                raise ValueError('音频包含 NaN/Inf')
            total+=len(data)
            d=data.astype(np.float64)
            channel_sums+=d.sum(axis=0)
            channel_energy+=(d*d).sum(axis=0)
            peaks=max(peaks,float(np.max(np.abs(data))))
            clipping+=int(np.count_nonzero(np.abs(data)>=1))
            if channels==2:
                cross+=float((d[:,0]*d[:,1]).sum())
    if not total:
        raise ValueError('空音频')
    rms=np.sqrt(channel_energy/total)
    dc=channel_sums/total
    correlation=None
    if channels==2:
        correlation=correlation_from_moments(cross, channel_energy, dc, total)
    return dict(sample_peak=peaks,clipped_samples=clipping,dc_offset=dc.tolist(),
                channel_rms=rms.tolist(),stereo_correlation=correlation,finite=True)

def inspect_rvc_raw(path):
    """Record the RVC file before Align or Mix. This record does not fail the job."""
    record=dict(role='rvc_raw',blocks_job=False,path_name=os.path.basename(path) if path else None,
                finite=False,silent=None,sample_peak=None,clipped_samples=None,channel_rms=None,findings=[])
    try:
        metrics=sample_metrics(path)
    except (OSError,ValueError) as exc:
        record['findings'].append('RVC 原始输出无法作为有限音频: '+str(exc))
        return record
    silent=max(metrics['channel_rms'])<1e-5
    record.update(finite=True,silent=silent,sample_peak=metrics['sample_peak'],
                  clipped_samples=metrics['clipped_samples'],channel_rms=metrics['channel_rms'])
    if silent:
        record['findings'].append('RVC 原始输出静音')
    if metrics['clipped_samples']:
        record['findings'].append('RVC 原始输出已削波')
    return record

def run_quality_control(master_wav_path,master_mp3_path,original_file_name,mode_name,model_name='N/A',
                        sync_offset_ms=0.,expected_duration=None,target_lufs=-11.,
                        true_peak_limit=-1.,alignment_path=None,stem_paths=None,extra_exports=None,
                        rvc_raw=None,polish_before=None):
    issues=[]
    failures=[]
    if mode_name == 'voice_conversion':
        if not alignment_path:
            failures.append('换声缺少必需对齐证据')
        names = {os.path.basename(p).lower() for p in stem_paths or []}
        for required in ('01_vocal_original.wav','02_vocal_cleaned.wav','03_vocal_aligned.wav','04_instrumental.wav'):
            if required not in names:
                failures.append('换声缺少必需分轨：'+required)
    meta=probe_metadata(master_wav_path)
    levels=measure_loudness_ebur128(master_wav_path)
    metrics=sample_metrics(master_wav_path)
    duration=meta['duration']
    if expected_duration is not None and abs(duration-expected_duration)>.1:
        failures.append('成品时长与输入偏差超过 100ms')
    if max(metrics['channel_rms'])<1e-5:
        failures.append('成品静音')
    if metrics['clipped_samples']:
        failures.append('成品存在削波采样')
    if max(abs(x) for x in metrics['dc_offset'])>.005:
        issues.append('DC 偏移超过 0.5%')
    if metrics['stereo_correlation'] is not None and metrics['stereo_correlation']<-.25:
        issues.append('立体声相位反相关，请试听单声道兼容性')
    if abs(levels['integrated_lufs']-target_lufs)>2.1:
        issues.append('响度偏离所选目标超过 2.1 LUFS')
    if levels['true_peak_dbtp']>true_peak_limit:
        failures.append('WAV 真实峰值超过设置上限')
    polish_comparison=None
    polish_trace=None
    polish_degraded_stages=[]
    has_polish_degradation=False
    if polish_before:
        from core.polish_qc import comparison_from_files, trace_metrics
        polish_comparison=comparison_from_files(polish_before, master_wav_path)
        for finding in polish_comparison['findings']:
            issues.append(finding['warning'])
        trace_dir = os.path.dirname(polish_before)
        polish_trace=trace_metrics(trace_dir, master_wav_path)

        stage_specs = [
            ("repair", "修损降噪 (Repair)", "执行削波修复与频谱去噪"),
            ("tone", "音色均衡 (Tone)", "执行共振衰减与频谱均衡匹配"),
            ("dynamics", "动态控制 (Dynamics)", "执行多频段压缩与动态平滑"),
            ("stereo", "立体声场 (Stereo)", "执行中侧声场增强与相位校正"),
        ]
        decision_path = os.path.join(trace_dir, "polish_decision.json")
        if os.path.isfile(decision_path):
            try:
                with open(decision_path, "r", encoding="utf-8") as f:
                    decision_data = json.load(f)
                if decision_data.get("has_degradation"):
                    has_polish_degradation = True
            except (OSError, ValueError):
                pass

        for stg, label, purpose in stage_specs:
            stage_dec_file = os.path.join(trace_dir, f"{stg}.decision.json")
            if os.path.isfile(stage_dec_file):
                try:
                    with open(stage_dec_file, "r", encoding="utf-8") as f:
                        sdata = json.load(f)
                    if sdata.get("degraded") or sdata.get("status") == "BYPASSED_BY_ERROR":
                        has_polish_degradation = True
                        warn_msg = sdata.get("warning") or sdata.get("error") or "处理异常"
                        polish_degraded_stages.append({
                            "stage": stg,
                            "name": label,
                            "intended_action": purpose,
                            "reason": warn_msg,
                            "error_type": sdata.get("error_type", "Exception"),
                            "source_preserved": True,
                            "retryable": True,
                        })
                        issues.append(f"精修阶段【{label}】异常降级：{warn_msg}（已保留前一阶段音频，可按需重试）")
                except (OSError, ValueError):
                    pass
    exports={}
    stems={}
    for path in [master_wav_path,master_mp3_path]+list(extra_exports or []):
        if not path:
            continue
        info=probe_metadata(path)
        # Decode entire export, not just its container header.
        run([FFMPEG_EXE,'-v','error','-xerror','-i',path,'-map','0:a:0','-f','null','-'])
        measured=measure_loudness_ebur128(path)
        exports[os.path.basename(path)]=dict(metadata=info,**measured)
        if abs(info['duration']-duration)>.15:
            failures.append('导出时长异常: '+os.path.basename(path))
        if measured['true_peak_dbtp']>true_peak_limit:
            failures.append('导出真实峰值超限: '+os.path.basename(path))
    for path in stem_paths or []:
        info=probe_metadata(path)
        stem_metrics=sample_metrics(path)
        stems[os.path.basename(path)] = dict(metadata=info, **stem_metrics)
        if stem_metrics['clipped_samples']:
            issues.append('分轨采样超过满幅，导入 DAW 时需降低增益: '+os.path.basename(path))
        if mode_name=='voice_conversion' and 'vocal' in os.path.basename(path).lower() and max(stem_metrics['channel_rms'])<1e-5:
            failures.append('换声必需人声音轨静音: '+os.path.basename(path))
        if abs(info['duration']-duration)>.1:
            failures.append('分轨时长异常: '+os.path.basename(path))
    alignment=None
    if alignment_path:
        with open(alignment_path,encoding='utf-8-sig') as f:
            alignment=json.load(f)
        residual=max(abs(float(alignment.get('residual_ms', 0.0))), float(alignment.get('max_local_residual_ms', 0.0)))
        if not alignment.get('verified'):
            issues.append('对齐相关性不足，不能确认同步；需试听')
        if residual>30:
            failures.append('对齐后残余误差超过 30ms（实测 %.2fms）' % residual)
        elif residual>=10:
            issues.append('对齐后残余误差 10~30ms，未达到小于10ms的目标（实测 %.2fms）' % residual)
    status='FAIL' if failures else ('WARN' if issues else 'PASS')
    audio_file_valid = (len(failures) == 0)
    audio_metrics_passed = (len(failures) == 0)
    processing_integrity = 'DEGRADED' if (has_polish_degradation or any('降级' in str(x) for x in issues)) else 'INTACT'
    stage_execution_intact = (processing_integrity == 'INTACT')
    model_inference_ran = (rvc_raw is not None and bool(rvc_raw.get('finite'))) if mode_name == 'voice_conversion' else True
    data=dict(status=status,
              audio_file_valid=audio_file_valid,
              audio_metrics_passed=audio_metrics_passed,
              stage_execution_intact=stage_execution_intact,
              processing_integrity=processing_integrity,
              model_inference_ran=model_inference_ran,
              quality_acceptance='READY_FOR_EXTERNAL_VALIDATION',
              listening_quality='not_assessed',
              degraded_stages=polish_degraded_stages,
              timestamp=datetime.datetime.now().isoformat(),original_file=original_file_name,
              mode=mode_name,model=model_name,duration_seconds=duration,**levels,**metrics,
              correction_offset_ms=sync_offset_ms,alignment=alignment,issues=issues,failures=failures,exports=exports,stems=stems)
    data['requested_lufs'] = target_lufs
    if alignment is not None:
        data['alignment_offset_ms'] = float(alignment.get('offset_ms', sync_offset_ms))
        data['residual_offset_ms'] = float(alignment.get('residual_ms', 0.0))
        data['max_local_residual_ms'] = float(alignment.get('max_local_residual_ms', 0.0))
        data['alignment_verified'] = bool(alignment.get('verified', False))
    if rvc_raw is not None:
        data['rvc_raw']=rvc_raw
    if polish_comparison is not None:
        data['polish_comparison']=polish_comparison
    if polish_trace is not None:
        data['polish_trace']=polish_trace
    try:
        with open(master_wav_path+'.json', encoding='utf-8') as handle:
            mastering = json.load(handle)
        data['effective_lufs'] = mastering.get('effective_lufs',target_lufs)
    except (OSError,ValueError):
        data['effective_lufs'] = target_lufs
    report='# 音乐处理质检报告\n\n'
    report+='- 综合质检状态：**%s**\n'%(status)
    report+='- 音频文件有效性：**已完整解码验证（有限浮点数值）**\n'
    report+='- 客观数值指标：**%s**\n'%('通过' if audio_metrics_passed else '未通过（存在超限指标）')
    report+='- 阶段执行完整度：%s\n'%('INTACT（全部阶段完整执行）' if stage_execution_intact else '**DEGRADED（存在异常降级）**')
    report+='- 模型推理运行：%s\n'%('已完成推理' if model_inference_ran else '未运行/已跳过')
    report+='- 人工音质听审：**未评估 (not_assessed - 客观指标通过不代表免审，音色/伪影需人工试听)**\n'
    report+='- 质量验收状态：**待外部/人工验收 (READY_FOR_EXTERNAL_VALIDATION)**\n'
    report+='- 输入：%s\n- 模式：%s\n- 模型：%s\n'%(original_file_name,mode_name,model_name)
    report+='- 时长：%.3fs\n- 响度：%.2f LUFS（目标 %.1f）\n- True Peak：%.2f dBTP（上限 %.1f）\n'%(
        duration,levels['integrated_lufs'],target_lufs,levels['true_peak_dbtp'],true_peak_limit)
    report+='- 削波采样：%s\n'%(metrics['clipped_samples'])
    if alignment is not None:
        report+='- 对齐整体修正量：%.2fms（全局时间平移量）\n'%float(alignment.get('offset_ms', sync_offset_ms))
        report+='- 对齐残余偏移：%.2fms（对齐后残余全局误差）\n'%float(alignment.get('residual_ms', 0.0))
        report+='- 对齐最大局部误差：%.2fms（局部时间窗最大偏差）\n'%float(alignment.get('max_local_residual_ms', 0.0))
        report+='- 对齐置信验证：%s%s\n'%(
            '已通过置信校验' if alignment.get('verified') else '未通过置信校验（相关性不足，需人工试听）',
            ('（相关系数 %.3f）'%alignment['correlation']) if alignment.get('correlation') is not None else ''
        )
    else:
        report+='- 校正量：%.2fms（校正量不是残余同步误差）\n'%(sync_offset_ms)
    report+='- 请求/有效响度目标：%.2f / %.2f LUFS\n' % (target_lufs,data['effective_lufs'])
    report+='\n'+('\n'.join('- '+x for x in failures+issues) or '客观检测通过；音色、伪影、扒谱准确度仍需试听及人工审阅。')
    report+='\n\n导出文件：\n'+ '\n'.join('- '+x for x in exports)+'\n'
    if polish_degraded_stages:
        report += '\n## 阶段执行与降级说明 (Processing Integrity)\n\n'
        report += '以下处理阶段因异常而自动降级。系统已安全保留前一阶段音频并阻止写入破坏数据；任务可按需重试：\n\n'
        for deg in polish_degraded_stages:
            report += f"- **{deg['name']}**：\n"
            report += f"  - 预定任务：{deg['intended_action']}\n"
            report += f"  - 降级原因：`{deg['reason']}`\n"
            report += f"  - 音频保护：{'已保留前一阶段有效音频，未破坏输出' if deg['source_preserved'] else '未保留'}\n"
            report += f"  - 重试建议：{'支持重新运行该阶段' if deg['retryable'] else '不可自动重试'}\n"
    if polish_comparison is not None:
        report+='\n## Polish before / after\n\n'
        report+='- 高频损失：%s\n- 动态塌陷：%s\n- 相位或相关性：%s\n- 削波：%s\n'%(
            '是' if polish_comparison['high_frequency_loss'] else '否',
            '是' if polish_comparison['dynamic_range_collapse'] else '否',
            '是' if polish_comparison['phase_degradation'] else '否',
            '是' if polish_comparison['clipping'] else '否')
        report+='\n'.join('- '+item['warning'] for item in polish_comparison['findings'])
        report+='\n' if polish_comparison['findings'] else ''
    if polish_trace:
        labels={'source':'原始','repaired':'修复后','tone':'Tone 后','dynamics':'Dynamics 后','stereo':'Stereo 后','master':'Master 后'}
        report+='\n## Polish stage levels\n\n'
        for row in polish_trace:
            label=labels.get(row['role'], row['role'])
            if not row.get('present'):
                report+='- %s：缺失\n'%label
            else:
                report+='- %s：peak %.4f，RMS %.4f\n'%(label, row['peak'], row['rms'])
    if rvc_raw is not None:
        report+='\n## RVC 原始输出\n\n'
        report+='- 检查点：Align/Mix 之前\n- 不改变任务成败：%s\n'%('是' if not rvc_raw.get('blocks_job') else '否')
        report+='- 母带削波采样：%s\n- RVC 原始削波采样：%s\n'%(metrics['clipped_samples'],rvc_raw.get('clipped_samples'))
        report+='- RVC 原始峰值：%s\n- RVC 原始静音：%s\n- RVC 原始有限：%s\n'%(
            rvc_raw.get('sample_peak'),rvc_raw.get('silent'),rvc_raw.get('finite'))
        report+='\n'.join('- '+item for item in rvc_raw.get('findings') or [])+('\n' if rvc_raw.get('findings') else '')
    return data,report

