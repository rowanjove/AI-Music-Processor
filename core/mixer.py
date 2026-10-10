import os
import subprocess
from core.config import FFMPEG_EXE
from core.processes import run
from core.analyzer import measure_loudness_ebur128, probe_metadata

def mix_stems(
    vocal_wav,
    inst_wav,
    output_mix_wav,
    vocal_gain_db=0.5,
    inst_gain_db=-1.0,
    add_reverb=False,
    skip_mix_compressor=False,
    adaptive_mix=None,
    mix_bus_profile='off',
):
    """
    智能自适应混音:
    - 伴奏微降 (-1dB) 让位，人声微推 (+0.5dB) 凸显
    - 默认不加 20ms/0.07 的 aecho；只有 add_reverb=True 才加
    - 默认仍用固定 acompressor；skip_mix_compressor=True 只去掉这一段
    - 按实测 LUFS 做有界增益匹配，混音保留峰值余量
    """
    if type(skip_mix_compressor) is not bool:
        raise ValueError("skip_mix_compressor 必须是布尔值")
    if adaptive_mix is not None and type(adaptive_mix) is not bool:
        raise ValueError('adaptive_mix 必须为布尔值')
    from core.bounded_voice_dsp import validate_profile
    validate_profile(mix_bus_profile)
    # Estimate balance from measured stems, with bounded corrections.
    import math
    def rms_dbfs(path):
        import numpy as np
        import soundfile as sf
        total=0.;count=0
        with sf.SoundFile(path) as source:
            for block in source.blocks(blocksize=32768,dtype='float64',always_2d=True):
                from core.processes import checkpoint
                checkpoint()
                if not np.isfinite(block).all():
                    raise ValueError('混音拒绝 NaN/Inf')
                total+=float(np.sum(block*block));count+=block.size
        if not count:
            raise ValueError('混音拒绝空输入')
        return 10*math.log10(total/count) if total else -math.inf
    level_metric='integrated_lufs'
    try:
        vocal_lufs = measure_loudness_ebur128(vocal_wav)['integrated_lufs']
        inst_lufs = measure_loudness_ebur128(inst_wav)['integrated_lufs']
    except ValueError:
        if adaptive_mix is None:
            raise
        # Both stems must use the same measured scale, never LUFS minus RMS.
        vocal_lufs,inst_lufs = rms_dbfs(vocal_wav),rms_dbfs(inst_wav)
        level_metric='rms_dbfs'
    requested_vocal_gain_db = vocal_gain_db
    if adaptive_mix is None:
        # Exact existing legacy balance, including its four-dB bound.
        vocal_gain_db += max(-4., min(4., inst_lufs - vocal_lufs - 2.))
    elif adaptive_mix and math.isfinite(inst_lufs) and math.isfinite(vocal_lufs):
        vocal_gain_db += max(-2., min(2., inst_lufs - vocal_lufs - 2.))
    duration = probe_metadata(inst_wav)['duration']
    adaptive_compressor = None
    if adaptive_mix is True and not skip_mix_compressor:
        import numpy as np
        import soundfile as sf
        total=0.;count=0;peak=0.
        with sf.SoundFile(vocal_wav) as source:
            for block in source.blocks(blocksize=32768,dtype='float64',always_2d=True):
                from core.processes import checkpoint
                checkpoint()
                if not np.isfinite(block).all():
                    raise ValueError('自适应压缩拒绝 NaN/Inf')
                total+=float(np.sum(block*block));count+=block.size
                peak=max(peak,float(np.max(np.abs(block))))
        rms=math.sqrt(total/count) if count else 0.
        crest_db=20*math.log10(peak/rms) if rms and peak else 0.
        adaptive_compressor={'threshold':max(.04,min(.18,rms*10**(vocal_gain_db/20)*1.5)),
                             'ratio':max(1.5,min(2.,1.5+(crest_db-6)/20)),
                             'attack':max(15.,min(30.,10+crest_db)),
                             'release':max(80.,min(160.,80+5*crest_db)),
                             'mix':.2,'measured_rms':rms,'measured_peak':peak,'crest_db':crest_db,
                             'dry_wet_maximum_attenuation_db':-20*math.log10(.8)}
    # 计算增益系数
    v_factor = round(10 ** (vocal_gain_db / 20.0), 3)
    i_factor = round(10 ** (inst_gain_db / 20.0), 3)

    # 人声混响滤镜链
    vocal_filter = f"volume={v_factor},aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    if add_reverb:
        # 轻量立体声空间胶水混响 (微延迟20ms，湿声比7%)
        vocal_filter += ",aecho=1:1:20:0.07"
    if not skip_mix_compressor:
        if adaptive_compressor is None:
            vocal_filter += ',acompressor=threshold=0.125:ratio=2.5:attack=15:release=80'
        else:
            vocal_filter += ',acompressor=' + ':'.join('%s=%.8f'%(key,adaptive_compressor[key]) for key in ('threshold','ratio','attack','release','mix'))

    inst_filter = f"volume={i_factor},aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

    filter_complex = (
        f"[0:a]{vocal_filter}[voc];"
        f"[1:a]{inst_filter}[inst];"
        f"[voc][inst]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,volume=-3dB,atrim=duration={duration}[mixed]"
    )

    cmd = [
        FFMPEG_EXE, "-y",
        "-i", vocal_wav,
        "-i", inst_wav,
        "-filter_complex", filter_complex,
        "-map", "[mixed]",
        "-c:a", "pcm_f32le",
        "-ar", "48000",
        output_mix_wav
    ]

    run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    )
    if adaptive_mix is not None:
        import json
        import uuid
        report_path = os.fspath(output_mix_wav) + '.mix.json'
        temporary = report_path + '.part.' + uuid.uuid4().hex
        try:
            with open(temporary, 'w', encoding='utf-8') as handle:
                json.dump({'schema_version': 1, 'adaptive_mix': adaptive_mix,
                           'level_metric': level_metric,
                           'vocal_lufs': vocal_lufs if level_metric == 'integrated_lufs' and math.isfinite(vocal_lufs) else None,
                           'instrumental_lufs': inst_lufs if level_metric == 'integrated_lufs' and math.isfinite(inst_lufs) else None,
                           'measured_vocal_level_db': vocal_lufs if math.isfinite(vocal_lufs) else None,
                           'measured_instrumental_level_db': inst_lufs if math.isfinite(inst_lufs) else None,
                           'vocal_gain_db': vocal_gain_db, 'instrumental_gain_db': inst_gain_db,
                           'adaptive_correction_db': vocal_gain_db-requested_vocal_gain_db,
                           'correction_limit_db': 2, 'mix_bus_profile': mix_bus_profile,
                           'adaptive_compressor': adaptive_compressor,
                           'quality_status': 'READY_FOR_EXTERNAL_VALIDATION'}, handle, indent=2, allow_nan=False)
            os.replace(temporary, report_path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    if mix_bus_profile == 'gentle':
        from core.bounded_voice_dsp import process
        bus_output = os.fspath(output_mix_wav) + '.bus.wav'
        process(output_mix_wav, bus_output, kind='bus')
        return bus_output
    return output_mix_wav
