"""Envelope correlation; positive lag means the converted vocal is early."""
import json
import math
import numpy as np
import soundfile as sf
from scipy import signal

def compute_envelope(samples, hop_length=64):
    if samples.ndim > 1:
        samples = np.mean(np.abs(samples), axis=1)
    return signal.convolve(np.abs(samples), np.ones(hop_length) / hop_length, mode='same', method='fft')

def _envelope(data, sr):
    calc_sr = 2000
    env = compute_envelope(data, max(1, int(sr * .005)))
    div = math.gcd(sr, calc_sr)
    env = signal.resample_poly(env, calc_sr // div, sr // div)
    return env - np.mean(env), calc_sr

def _lag(ref, converted, sr, max_lag_ms):
    a, rate = _envelope(ref, sr)
    b, _ = _envelope(converted, sr)
    # Ignore padded/cropped edges; they carry no musical timing information.
    margin = int(max_lag_ms * rate / 1000)
    if min(len(a), len(b)) > 2*margin + rate:
        a = a[margin:-margin]
        b = b[margin:-margin]
    if np.linalg.norm(a) * np.linalg.norm(b) < 1e-12:
        return 0., 0.
    corr = signal.correlate(a, b, method='fft')
    lags = signal.correlation_lags(len(a), len(b))
    mask = np.abs(lags) <= int(max_lag_ms * rate / 1000)
    candidate_lags = lags[mask]
    start_a = np.maximum(candidate_lags, 0)
    start_b = np.maximum(-candidate_lags, 0)
    length = np.minimum(len(a)-start_a, len(b)-start_b)
    def sums(data, start, count):
        prefix = np.concatenate(([0.], np.cumsum(data)))
        return prefix[start+count]-prefix[start]
    sum_a = sums(a, start_a, length)
    sum_b = sums(b, start_b, length)
    variance_a = sums(a*a, start_a, length)-sum_a*sum_a/length
    variance_b = sums(b*b, start_b, length)-sum_b*sum_b/length
    denominator = np.sqrt(np.maximum(variance_a*variance_b, 1e-24))
    scores = (corr[mask]-sum_a*sum_b/length)/denominator
    index = int(np.argmax(scores))
    lag = float(candidate_lags[index]) / rate * 1000
    confidence = float(np.clip(scores[index], -1., 1.))
    return lag, confidence

def align_vocal_timing(ref_vocal_path, rvc_vocal_path, output_aligned_path, max_lag_ms=300):
    ref, sr = sf.read(ref_vocal_path, always_2d=True, dtype='float32')
    data, data_sr = sf.read(rvc_vocal_path, always_2d=True, dtype='float32')
    if not np.isfinite(ref).all() or not np.isfinite(data).all():
        raise ValueError('人声音轨含 NaN/Inf')
    if data_sr != sr:
        div = math.gcd(sr, data_sr)
        data = signal.resample_poly(data, sr // div, data_sr // div, axis=0)
    lag, confidence = _lag(ref, data, sr, max_lag_ms)
    shift = round(lag * sr / 1000)
    # correlate(ref, converted): delayed conversion has NEGATIVE lag.
    if shift < 0:
        data = data[-shift:]
    elif shift > 0:
        data = np.pad(data, ((shift, 0), (0, 0)))
    data = data[:len(ref)]
    if len(data) < len(ref):
        data = np.pad(data, ((0, len(ref)-len(data)), (0, 0)))
    residual, final_confidence = _lag(ref, data, sr, max_lag_ms)
    def local_checks(audio):
        result = []
        for start in range(0, len(ref), sr * 15):
            a, b = ref[start:start+sr*15], audio[start:start+sr*15]
            if len(a) >= sr and np.sqrt(np.mean(a*a)) > .005:
                offset, score = _lag(a, b, sr, max_lag_ms)
                if score > .25:
                    result.append(dict(offset_ms=offset, confidence=score, center_sample=start+len(a)//2))
        return result
    local = local_checks(data)
    initial_local = local
    trusted = [x for x in local if x['confidence'] >= .5 and abs(x['offset_ms']) < max_lag_ms]
    warped = False
    safe_rate = len(trusted) >= 2 and all(
        abs((b['offset_ms']-a['offset_ms'])*sr/1000/(b['center_sample']-a['center_sample'])) <= .005
        for a,b in zip(trusted,trusted[1:]))
    if safe_rate and max(abs(x['offset_ms']) for x in trusted) > 10:
        # Gentle continuous timing correction, bounded by measured confident anchors.
        candidate = np.empty_like(data)
        anchors = [x['center_sample'] for x in trusted]
        corrections = [x['offset_ms']*sr/1000 for x in trusted]
        # Limit float64 positions/interpolation buffers to 15 seconds.
        for start in range(0,len(data),sr*15):
            end = min(start+sr*15,len(data))
            positions = np.arange(start,end,dtype=np.float64)
            requested = positions-np.interp(positions,anchors,corrections)
            low = max(0,int(np.floor(requested.min()))-1)
            high = min(len(data),int(np.ceil(requested.max()))+2)
            source_positions = np.arange(low,high,dtype=np.float64)
            for channel in range(data.shape[1]):
                candidate[start:end,channel] = np.interp(requested,source_positions,data[low:high,channel],left=0,right=0)
        candidate_local = local_checks(candidate)
        if ([x['center_sample'] for x in candidate_local] == [x['center_sample'] for x in local]
                and max(abs(x['offset_ms']) for x in candidate_local) < max(abs(x['offset_ms']) for x in local)):
            data, local, warped = candidate, candidate_local, True
            residual, final_confidence = _lag(ref, data, sr, max_lag_ms)
    sf.write(output_aligned_path, data, sr, subtype='FLOAT')
    metrics = dict(correction_ms=lag, residual_ms=residual, confidence=final_confidence,
                   local_checks=local, initial_local_checks=initial_local, local_timewarp=warped,
                   max_local_residual_ms=max([abs(x['offset_ms']) for x in local] or [abs(residual)]),
                   verified=confidence >= .25 and final_confidence >= .25 and abs(lag) < max_lag_ms)
    with open(str(output_aligned_path)+'.json', 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2)
    return output_aligned_path, lag

