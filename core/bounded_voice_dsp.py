"""Opt-in streaming DSP. Measured gain reduction is capped at 2 dB.

This is an engineering profile, not a claim of listening-quality improvement.
All filters retain state across blocks and preserve sample count and channels.
"""
import json
import os
import uuid
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, lfilter

PROFILES = ('off', 'gentle')


def validate_profile(value):
    if value not in PROFILES or not isinstance(value, str):
        raise ValueError('DSP profile 必须为 off 或 gentle')
    return value


def process(source, dest, *, kind='post', profile='gentle'):
    validate_profile(profile)
    if profile == 'off':
        return os.fspath(source)
    if kind not in ('post', 'bus'):
        raise ValueError('未知 DSP 阶段')
    if os.path.realpath(source) == os.path.realpath(dest):
        raise ValueError('DSP 不得覆盖源文件')
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    temporary = os.fspath(dest) + '.part.' + uuid.uuid4().hex
    minimum_gain = 10 ** (-2 / 20)
    maximum_gr = 0.
    peak_in = peak_out = 0.
    frames = 0
    try:
        with sf.SoundFile(source) as inp:
            rate, channels = inp.samplerate, inp.channels
            if rate < 16000 or channels not in (1, 2) or not len(inp):
                raise ValueError('DSP 要求非空、16kHz以上 mono/stereo 输入')
            high = butter(2, 65 if kind == 'post' else 25, fs=rate, btype='highpass', output='sos')
            band = butter(2, [min(4500, rate * .25), min(8500, rate * .45)], fs=rate, btype='bandpass', output='sos')
            high_state = np.zeros((len(high), 2, channels))
            band_state = np.zeros((len(band), 2, channels))
            envelope_state = np.zeros((1, 1))
            alpha = float(np.exp(-1 / (.02 * rate)))
            with sf.SoundFile(temporary, 'w', samplerate=rate, channels=channels, format='WAV', subtype='FLOAT') as out:
                for audio in inp.blocks(blocksize=32768, dtype='float64', always_2d=True):
                    from core.processes import checkpoint
                    checkpoint()
                    if not np.isfinite(audio).all():
                        raise ValueError('DSP 拒绝 NaN/Inf')
                    peak_in = max(peak_in, float(np.max(np.abs(audio))))
                    shaped, high_state = sosfilt(high, audio, axis=0, zi=high_state)
                    if kind == 'post':
                        sibilance, band_state = sosfilt(band, shaped, axis=0, zi=band_state)
                        # Subtract at most 1 dB of the measured high-frequency band.
                        shaped = shaped - (1 - 10 ** (-1 / 20)) * sibilance
                    envelope, envelope_state = lfilter([1-alpha], [1, -alpha], np.max(np.abs(shaped), axis=1)[:, None], axis=0, zi=envelope_state)
                    above = np.maximum(envelope[:, 0] / .18, 1.)
                    gain = np.maximum(minimum_gain, above ** (-1/3))
                    maximum_gr = max(maximum_gr, float(np.max(-20*np.log10(gain))))
                    shaped *= gain[:, None]
                    # Soft knee only above .9; bounded without normalizing quiet audio.
                    magnitude = np.abs(shaped)
                    shaped = np.sign(shaped) * np.where(magnitude <= .9, magnitude, .9 + .08 * np.tanh((magnitude-.9)/.08))
                    if not np.isfinite(shaped).all():
                        raise ValueError('DSP 产生非有限输出')
                    peak_out = max(peak_out, float(np.max(np.abs(shaped))))
                    out.write(shaped)
                    frames += len(shaped)
            if frames != len(inp):
                raise RuntimeError('DSP 帧数不一致')
        from core.processes import checkpoint
        checkpoint()
        os.replace(temporary, dest)
        receipt = {'schema_version': 1, 'profile': profile, 'kind': kind, 'frames': frames,
                   'sample_rate': rate, 'channels': channels, 'peak_in': peak_in, 'peak_out': peak_out,
                   'maximum_compressor_gain_reduction_db': maximum_gr, 'gain_reduction_limit_db': 2,
                   'quality_status': 'READY_FOR_EXTERNAL_VALIDATION'}
        from core.storage import sha256_file
        receipt.update(source_sha256=sha256_file(source), output_sha256=sha256_file(dest))
        receipt_path = os.fspath(dest) + '.dsp.json'
        receipt_temp = receipt_path + '.part.' + uuid.uuid4().hex
        try:
            with open(receipt_temp, 'w', encoding='utf-8') as handle:
                json.dump(receipt, handle, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(receipt_temp, receipt_path)
        finally:
            if os.path.exists(receipt_temp):
                os.remove(receipt_temp)
        return os.fspath(dest)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
