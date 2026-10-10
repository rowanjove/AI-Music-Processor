"""Measured two-pass loudness mastering; verify every encoded format."""
import json
import os
import tempfile
from core.config import FFMPEG_EXE
from core.analyzer import loudnorm_measure, measure_loudness_ebur128
from core.processes import run

def master_bus(input_mix_wav, output_master_wav, output_master_mp3=None, target_lufs=-11.,
               true_peak_limit=-1., sample_rate=48000, output_flac=None, output_channels=2):
    input_mix_wav,output_master_wav=map(os.fspath,(input_mix_wav,output_master_wav))
    output_master_mp3=os.fspath(output_master_mp3) if output_master_mp3 is not None else None
    output_flac=os.fspath(output_flac) if output_flac is not None else None
    if type(output_channels) is not int or output_channels not in (1,2):
        raise ValueError('母带输出声道数只能为整数 1/2')
    if output_channels==2:
        return _master_bus_impl(input_mix_wav,output_master_wav,output_master_mp3,target_lufs,
                                true_peak_limit,sample_rate,output_flac,output_channels)
    directory=os.path.dirname(os.path.abspath(output_master_wav));os.makedirs(directory,exist_ok=True)
    descriptor,mono=tempfile.mkstemp(prefix='.master-mono-',suffix='.wav',dir=directory);os.close(descriptor)
    try:
        run([FFMPEG_EXE,'-y','-v','error','-i',input_mix_wav,'-vn','-ac','1','-c:a','pcm_f32le',mono])
        return _master_bus_impl(mono,output_master_wav,output_master_mp3,target_lufs,
                                true_peak_limit,sample_rate,output_flac,output_channels)
    finally:
        if os.path.exists(mono):os.unlink(mono)


def _master_bus_impl(input_mix_wav, output_master_wav, output_master_mp3=None, target_lufs=-11.,
                     true_peak_limit=-1., sample_rate=48000, output_flac=None,output_channels=2):
    if sample_rate not in (44100,48000):
        raise ValueError('输出采样率仅支持 44100/48000')
    measured = loudnorm_measure(input_mix_wav, target_lufs, true_peak_limit-.3)
    requested = target_lufs
    estimated_reduction = measured['input_tp'] + target_lufs - measured['input_i'] - true_peak_limit
    if estimated_reduction > 5:
        target_lufs -= min(2., estimated_reduction-5)
    stats = loudnorm_measure(input_mix_wav, target_lufs, true_peak_limit-.3)
    chain = ('loudnorm=I={i}:TP={tp}:LRA=11:measured_I={mi}:measured_TP={mt}:'
             'measured_LRA={ml}:measured_thresh={th}:offset={off}:linear=true').format(
                 i=target_lufs,tp=true_peak_limit-.3,mi=stats['input_i'],mt=stats['input_tp'],
                 ml=stats['input_lra'],th=stats['input_thresh'],off=stats['target_offset'])
    run([FFMPEG_EXE,'-y','-v','error','-i',input_mix_wav,'-vn','-af',chain,'-c:a','pcm_s24le',
         '-ar',str(sample_rate),'-ac',str(output_channels),output_master_wav])
    exports = [output_master_wav]
    if output_master_mp3:
        run([FFMPEG_EXE,'-y','-v','error','-i',output_master_wav,'-c:a','libmp3lame','-b:a','320k',output_master_mp3])
        exports.append(output_master_mp3)
    if output_flac:
        run([FFMPEG_EXE,'-y','-v','error','-i',output_master_wav,'-c:a','flac',output_flac])
        exports.append(output_flac)
    correction = {}
    for path in exports:
        for attempt in range(3):
            levels = measure_loudness_ebur128(path)
            if levels['true_peak_dbtp'] <= true_peak_limit:
                break
            if attempt == 2:
                raise RuntimeError('母带真实峰值自动修复失败: '+path)
            db = true_peak_limit-levels['true_peak_dbtp']-.15
            temporary = os.path.splitext(path)[0]+'.qc-fix'+os.path.splitext(path)[1]
            codec = ['-c:a','libmp3lame','-b:a','320k'] if path.endswith('.mp3') else (
                ['-c:a','flac'] if path.endswith('.flac') else ['-c:a','pcm_s24le'])
            run([FFMPEG_EXE,'-y','-v','error','-i',path,'-af','volume=%sdB'%db]+codec+[temporary])
            os.replace(temporary,path)
            correction[path] = correction.get(path,0)+db
    with open(output_master_wav+'.json','w',encoding='utf-8') as f:
        json.dump(dict(requested_lufs=requested,effective_lufs=target_lufs,
                       estimated_peak_reduction_db=estimated_reduction,peak_corrections_db=correction,
                       relaxed=target_lufs!=requested,output_channels=output_channels), f,ensure_ascii=False,indent=2)
    return output_master_wav,output_master_mp3

