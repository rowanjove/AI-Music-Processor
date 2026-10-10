"""Conservative, opt-in stable-note correction before voice conversion.

Observed F0 selects notes; it is never flattened or used to synthesize a new
voice. A constant cents shift preserves each accepted note's contour. Glides,
unvoiced regions and uncertain keys bypass. Listening remains external.
"""
import json
import math
import os
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

ALGORITHM_VERSION = 'stable-note-cents-v1'
_MODES = {'light': (.35, 35.), 'medium': (.65, 45.), 'strong': (.9, 50.)}
_NAMES = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
_SCALES = {'major': (0, 2, 4, 5, 7, 9, 11), 'minor': (0, 2, 3, 5, 7, 8, 10)}


def _check(cancel):
    if cancel is not None:
        if callable(cancel):
            cancel()
        else:
            cancel.check()


def _scale(key):
    if not isinstance(key, str):
        raise ValueError('Pitch key must be a tonic and major/minor scale')
    parts = key.strip().split()
    aliases = {'Db': 'C#', 'Eb': 'D#', 'Gb': 'F#', 'Ab': 'G#', 'Bb': 'A#'}
    tonic = aliases.get(parts[0], parts[0]) if parts else ''
    kind = parts[1].lower() if len(parts) == 2 else 'major'
    if len(parts) not in (1, 2) or tonic not in _NAMES or kind not in _SCALES:
        raise ValueError('Pitch key must be a tonic and major/minor scale')
    root = _NAMES.index(tonic)
    return {'key': tonic + ' ' + kind, 'pitch_classes': sorted((root + step) % 12 for step in _SCALES[kind])}


def _observe(mono, rate):
    import pyworld
    from scipy.signal import resample_poly
    divisor = math.gcd(rate, 16000)
    audio = np.ascontiguousarray(resample_poly(mono, 16000 // divisor, rate // divisor), dtype=np.float64)
    raw, times = pyworld.dio(audio, 16000, f0_floor=65, f0_ceil=1000, frame_period=10)
    hz = pyworld.stonemask(audio, raw, times, 16000)
    confidence = np.zeros(len(hz))
    for index in np.flatnonzero(hz > 0):
        centre = int(times[index] * 16000)
        chunk = audio[max(0, centre - 480):min(len(audio), centre + 480)]
        lag = int(round(16000 / hz[index]))
        if len(chunk) < lag * 3 or np.mean(chunk * chunk) < 1e-10:
            continue
        left, right = chunk[:-lag], chunk[lag:]
        denominator = np.linalg.norm(left) * np.linalg.norm(right)
        confidence[index] = max(0., float(np.dot(left, right) / denominator)) if denominator else 0.
    valid = (hz >= 65) & (hz <= 1000) & (confidence >= .65) & np.isfinite(hz)
    midi = np.full(len(hz), np.nan)
    midi[valid] = 69 + 12 * np.log2(hz[valid] / 440)
    return times, hz, midi, confidence


def _detect_scale(midi):
    """Group relative major/minor ambiguity by their identical note sets."""
    values = midi[np.isfinite(midi)]
    if len(values) < 100 or len(set(np.round(values).astype(int) % 12)) < 4:
        return None, 0.
    histogram = np.bincount(np.round(values).astype(int) % 12, minlength=12).astype(float)
    histogram /= histogram.sum()
    groups = {}
    for tonic in _NAMES:
        for kind in _SCALES:
            scale = _scale(tonic + ' ' + kind)
            classes = tuple(scale['pitch_classes'])
            # A scale must cover observations; tonic strength breaks relative
            # labels without pretending they are distinct pitch-class evidence.
            score = float(histogram[list(classes)].sum())
            preference = float(histogram[_NAMES.index(tonic)])
            if classes not in groups or preference > groups[classes][2]:
                groups[classes] = (score, scale, preference)
    ranked = sorted(groups.values(), key=lambda item: (item[0], item[2]), reverse=True)
    margin = ranked[0][0] - ranked[1][0]
    confidence = float(np.clip(margin / .15, 0, 1))
    if ranked[0][0] < .9 or margin < .06:
        return None, confidence
    return ranked[0][1], confidence


def _notes(times, midi, confidence, scale, mode):
    amount, bound = _MODES[mode]
    targets = np.full(len(midi), np.nan)
    for index in np.flatnonzero(np.isfinite(midi)):
        base = int(np.round(midi[index]))
        candidates = [note for note in range(base - 2, base + 3) if note % 12 in scale['pitch_classes']]
        targets[index] = min(candidates, key=lambda note: abs(note - midi[index]))
    decisions = []
    start = 0
    while start < len(targets):
        if not np.isfinite(targets[start]):
            start += 1
            continue
        end = start + 1
        while end < len(targets) and targets[end] == targets[start]:
            end += 1
        duration = (end - start) * .01
        values = midi[start:end] * 100
        if duration >= .18:
            median = float(np.median(values))
            deviation = float(targets[start] * 100 - median)
            spread = float(np.percentile(values, 90) - np.percentile(values, 10))
            slope = float(np.polyfit(times[start:end] - times[start], values, 1)[0])
            # A glide or chromatic/non-scale note is intentionally preserved.
            if 18 <= abs(deviation) <= 50 and spread <= 85 and abs(slope) <= 20:
                cents = float(np.clip(deviation * amount, -bound, bound))
                decisions.append({'start_seconds': float(times[start]),
                                  'end_seconds': float(times[end - 1] + .01),
                                  'target_midi': int(targets[start]), 'observed_midi': median / 100,
                                  'observed_error_cents': deviation, 'applied_cents': cents,
                                  'confidence': float(np.mean(confidence[start:end])),
                                  'spread_cents': spread, 'trend_cents_per_second': slope})
        start = end
    return decisions


def correct_pitch_file(source, dest, mode='off', key=None, report_path=None, cancel=None):
    if mode not in ('off', *_MODES):
        raise ValueError('Unknown pitch correction mode')
    requested_scale = _scale(key) if key is not None else None
    source, dest = os.fspath(source), os.fspath(dest)
    report_path = os.fspath(report_path or (str(Path(dest).with_suffix('')) + '.pitch.json'))
    if os.path.abspath(dest) == os.path.abspath(report_path):
        raise ValueError('Pitch audio and report must use separate paths')
    if os.path.abspath(source) in (os.path.abspath(dest), os.path.abspath(report_path)):
        raise ValueError('Pitch correction cannot overwrite its source')
    _check(cancel)
    audio, rate = sf.read(source, dtype='float32', always_2d=True)
    if rate <= 0 or (mode != 'off' and rate < 16000) or not len(audio) or not np.isfinite(audio).all():
        raise ValueError('Pitch correction requires finite audio sampled at least 16 kHz')
    report = {'schema_version': 1, 'algorithm': ALGORITHM_VERSION, 'mode': mode,
              'status': 'BYPASSED', 'reason': 'disabled', 'sample_rate': rate,
              'frames': len(audio), 'channels': audio.shape[1], 'key': None,
              'key_confidence': None, 'key_source': 'explicit' if key is not None else 'auto',
              'corrections': [], 'listening_quality': 'READY_FOR_EXTERNAL_VALIDATION'}
    output = audio.copy()
    if mode != 'off':
        times, hz, midi, confidence = _observe(np.mean(audio, axis=1), rate)
        _check(cancel)
        report['f0_observation'] = {'extractor': 'pyworld.dio+stonemask', 'frame_period_ms': 10,
                                    'voiced_frames': int(np.isfinite(midi).sum()), 'total_frames': len(hz),
                                    'confidence_gate': .65}
        scale, key_confidence = (requested_scale, 1.) if requested_scale else _detect_scale(midi)
        report['key_confidence'] = key_confidence
        report['reason'] = 'uncertain_key_or_voicing'
        if scale is not None:
            report['key'] = scale['key']
            report['pitch_classes'] = scale['pitch_classes']
            corrections = _notes(times, midi, confidence, scale, mode)
            report['reason'] = 'no_stable_obvious_deviation'
            if corrections:
                import librosa
                for decision in corrections:
                    _check(cancel)
                    start = max(0, int(round(decision['start_seconds'] * rate)))
                    end = min(len(audio), int(round(decision['end_seconds'] * rate)))
                    # Additional context is analyzed by the phase vocoder, but
                    # only the note interval is blended into the source.
                    pad = int(.06 * rate)
                    left, right = max(0, start - pad), min(len(audio), end + pad)
                    shifted = np.stack([librosa.effects.pitch_shift(
                        audio[left:right, channel], sr=rate, n_steps=decision['applied_cents'] / 100,
                        res_type='kaiser_fast') for channel in range(audio.shape[1])], axis=1)
                    shifted = shifted[start-left:end-left]
                    if shifted.shape != audio[start:end].shape or not np.isfinite(shifted).all():
                        raise ValueError('Pitch shifter returned invalid shape/samples')
                    peak = float(np.max(np.abs(shifted)))
                    ceiling = max(1., float(np.max(np.abs(audio[start:end]))))
                    gain = min(1., ceiling / peak) if peak else 1.
                    shifted *= gain
                    fade = min(int(.03 * rate), (end - start) // 2)
                    weights = np.ones(end - start)
                    if fade:
                        ramp = .5 - .5 * np.cos(np.linspace(0, np.pi, fade))
                        weights[:fade], weights[-fade:] = ramp, ramp[::-1]
                    output[start:end] = audio[start:end] * (1 - weights[:, None]) + shifted * weights[:, None]
                    decision['safety_gain'] = gain
                report.update(status='APPLIED', reason='stable_notes_corrected', corrections=corrections)
    _check(cancel)
    for path in (dest, report_path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    suffix = '.part.' + uuid.uuid4().hex
    try:
        sf.write(dest + suffix, output, rate, format='WAV', subtype='FLOAT')
        with open(report_path + suffix, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
        _check(cancel)
        os.replace(report_path + suffix, report_path)
        os.replace(dest + suffix, dest)
    finally:
        for path in (dest + suffix, report_path + suffix):
            if os.path.isfile(path):
                os.remove(path)
    return {'wav': dest, 'decision': report_path, 'report': report}
