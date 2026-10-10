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
    if polish_before:
        from core.polish_qc import comparison_from_files, trace_metrics
        polish_comparison=comparison_from_files(polish_before, master_wav_path)
        for finding in polish_comparison['findings']:
            issues.append(finding['warning'])
        polish_trace=trace_metrics(os.path.dirname(polish_before), master_wav_path)
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
        residual=max(abs(alignment['residual_ms']),alignment.get('max_local_residual_ms',0))
        if not alignment.get('verified'):
            issues.append('对齐相关性不足，不能确认同步；需试听')
        if residual>30:
            failures.append('对齐后残余误差超过 30ms')
        elif residual>=10:
            issues.append('对齐后残余误差 10~30ms，未达到小于10ms的目标')
    status='FAIL' if failures else ('WARN' if issues else 'PASS')
    data=dict(status=status,timestamp=datetime.datetime.now().isoformat(),original_file=original_file_name,
              mode=mode_name,model=model_name,duration_seconds=duration,**levels,**metrics,
              correction_offset_ms=sync_offset_ms,alignment=alignment,issues=issues,failures=failures,exports=exports,stems=stems)
    data['requested_lufs'] = target_lufs
    if rvc_raw is not None:
        data['rvc_raw']=rvc_raw
    if mode_name=='polish':
        data['quality_acceptance']='READY_FOR_EXTERNAL_VALIDATION'
        data['listening_quality']='not_assessed'
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
    report+='- 状态：**%s**\n- 输入：%s\n- 模式：%s\n- 模型：%s\n'%(status,original_file_name,mode_name,model_name)
    report+='- 时长：%.3fs\n- 响度：%.2f LUFS（目标 %.1f）\n- True Peak：%.2f dBTP（上限 %.1f）\n'%(
        duration,levels['integrated_lufs'],target_lufs,levels['true_peak_dbtp'],true_peak_limit)
    report+='- 削波采样：%s\n- 校正量：%.2fms（校正量不是残余同步误差）\n'%(metrics['clipped_samples'],sync_offset_ms)
    report+='- 请求/有效响度目标：%.2f / %.2f LUFS\n' % (target_lufs,data['effective_lufs'])
    report+='\n'+('\n'.join('- '+x for x in failures+issues) or '客观检测通过；音色、伪影、扒谱准确度仍需试听及人工审阅。')
    report+='\n\n导出文件：\n'+ '\n'.join('- '+x for x in exports)+'\n'
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

