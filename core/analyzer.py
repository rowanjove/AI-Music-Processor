import json
import math
import re
from core.config import FFMPEG_EXE, FFPROBE_EXE
from core.processes import run

def probe_metadata(audio_path):
    result = run([FFPROBE_EXE, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(audio_path)])
    data = json.loads(result.stdout.decode('utf-8'))
    stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), None)
    if not stream:
        raise ValueError('文件没有可解码音轨: ' + str(audio_path))
    duration = float(data.get('format', {}).get('duration', stream.get('duration', 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('音频时长无效: ' + str(audio_path))
    return dict(duration=duration, sample_rate=int(stream['sample_rate']), channels=int(stream['channels']),
                bit_rate=int(data.get('format', {}).get('bit_rate', 0)),
                bit_depth=int(stream.get('bits_per_raw_sample') or stream.get('bits_per_sample') or 0),
                codec=stream.get('codec_name'))

def loudnorm_measure(audio_path, target=-11, peak=-1.2):
    result = run([FFMPEG_EXE, '-hide_banner', '-nostats', '-i', str(audio_path), '-vn', '-af',
                  'loudnorm=I=%s:TP=%s:LRA=11:print_format=json' % (target, peak), '-f', 'null', '-'])
    blocks = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', result.stderr.decode('utf-8', errors='replace'))
    if not blocks:
        raise RuntimeError('FFmpeg 未返回响度测量结果')
    values = {k: (float(v) if k != 'normalization_type' else v) for k, v in json.loads(blocks[-1]).items()}
    if not all(math.isfinite(values[k]) for k in ('input_i', 'input_tp', 'input_thresh')):
        raise ValueError('音频为静音或响度不可测；不能生成合格母带')
    return values

def measure_loudness_ebur128(audio_path):
    values = loudnorm_measure(audio_path)
    return dict(integrated_lufs=values['input_i'], true_peak_dbtp=values['input_tp'], lra=values['input_lra'])

def standardize_to_48k_float(input_path, output_path, input_channel_mode='auto'):
    if input_channel_mode not in ('auto','mono','stereo'):
        raise ValueError('输入声道模式无效')
    # Models use a stereo internal contract. An explicit mono choice mixes the
    # source first, then duplicates it without the default mono upmix gain.
    mapping=['-af','aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0'] if input_channel_mode=='mono' else []
    run([FFMPEG_EXE, '-y', '-v', 'error', '-i', str(input_path), '-map', '0:a:0', '-vn', '-c:a', 'pcm_f32le',
         *mapping,'-ar', '48000', '-ac', '2', str(output_path)])
    probe_metadata(output_path)
    return output_path

def analyze_audio(audio_path, detailed_analysis=True):
    """Objective full-file levels plus musical estimates from first 90 seconds."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    if type(detailed_analysis) is not bool:raise ValueError('详细分析开关必须是布尔值')
    metadata = probe_metadata(audio_path)
    if not detailed_analysis:
        result=dict(metadata,**measure_loudness_ebur128(audio_path),bpm_estimate=None,key_estimate=None,
                    key_confidence=None,musical_analysis_seconds=0,
                    note='用户关闭 BPM/调性估计；必要的电平与安全分析仍执行')
        from core.polish_analysis import attach_polish_analysis
        result=attach_polish_analysis(result,audio_path)
        if result.get('repair',{}).get('nan_inf'):raise ValueError('音频包含 NaN/Inf')
        return result
    import librosa
    with sf.SoundFile(audio_path) as f:
        sample = f.read(min(len(f), f.samplerate*90), dtype='float32', always_2d=True)
        sr = f.samplerate
    mono = sample.mean(axis=1)
    if not np.isfinite(mono).all():
        raise ValueError('音频包含 NaN/Inf')
    divisor = math.gcd(sr, 22050)
    mono = resample_poly(mono, 22050//divisor, sr//divisor)
    tempo = float(librosa.beat.tempo(y=mono, sr=22050)[0])
    chroma = librosa.feature.chroma_stft(y=mono, sr=22050).mean(axis=1)
    major = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
    minor = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
    candidates = [(float(np.corrcoef(chroma,np.roll(profile,shift))[0,1]),shift,mode)
                  for profile,mode in [(major,'major'),(minor,'minor')] for shift in range(12)]
    score, shift, mode = max(candidates)
    result = dict(metadata, **measure_loudness_ebur128(audio_path), bpm_estimate=tempo,
                  key_estimate=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][shift]+' '+mode,
                  key_confidence=score, musical_analysis_seconds=min(metadata['duration'],90),
                  note='BPM/调性是估计值；MIDI 节拍以用户设置为准')
    from core.polish_analysis import attach_polish_analysis
    result = attach_polish_analysis(result, audio_path)
    if result.get('repair', {}).get('nan_inf'):
        raise ValueError('音频包含 NaN/Inf')
    return result

