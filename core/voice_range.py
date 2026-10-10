"""Read-only bounded F0 range observations and explicit profile suggestions."""
import json
import math
import os
from pathlib import Path

import numpy as np
import soundfile as sf

from core.pitch_correction import _observe

ALGORITHM_VERSION = 'bounded-vocal-range-dio-v1'


def _check(cancel):
    if cancel is not None:
        if callable(cancel):
            cancel()
        elif hasattr(cancel, 'check'):
            cancel.check()
        else:
            cancel.check_cancel()


def analyze_voice_range(path, source_kind='mixed_music', target_range=None, cancel=None, report_path=None):
    if source_kind not in ('mixed_music', 'isolated_vocal', 'explicit_vocal_only'):
        raise ValueError('Unknown voice range source kind')
    _check(cancel)
    observations, periodicity, clips = [], [], []
    total_frames = 0
    with sf.SoundFile(path) as handle:
        rate, frames = handle.samplerate, len(handle)
        if not 16000 <= rate <= 192000 or frames <= 0 or handle.channels not in (1, 2):
            raise ValueError('Voice range requires finite-sized mono/stereo audio at 16–192 kHz')
        count = min(3, max(1, math.ceil(frames/(rate * 8))))
        window = min(int(rate * 8), frames//count)
        selected = np.linspace(0, frames-window, count).astype(int).tolist()
        for start in selected:
            _check(cancel)
            handle.seek(start)
            audio = handle.read(window, dtype='float32', always_2d=True)
            if not np.isfinite(audio).all():
                raise ValueError('Voice range input contains nonfinite samples')
            _, hz, midi, confidence = _observe(np.mean(audio, axis=1), rate)
            valid = np.isfinite(midi)
            observations.extend(hz[valid])
            periodicity.extend(confidence[valid])
            total_frames += len(hz)
            clips.append({'start_seconds': start/rate, 'duration_seconds': len(audio)/rate,
                          'voiced_frames': int(valid.sum()), 'total_frames': len(hz)})
    _check(cancel)
    ratio = len(observations)/total_frames if total_frames else 0.
    report = {'schema_version': 1, 'algorithm': ALGORITHM_VERSION, 'source_kind': source_kind,
              'extractor': 'pyworld.dio+stonemask', 'clips': clips,
              'observation_role': 'periodic_audio' if source_kind == 'mixed_music' else 'vocal_f0',
              'sampled_seconds': sum(clip['duration_seconds'] for clip in clips),
              'voiced_frames': len(observations), 'voiced_ratio': ratio,
              'mean_periodicity': float(np.mean(periodicity)) if periodicity else None,
              'observed_range_hz': None, 'observed_range_midi': None,
              'suggestion': {'semitones': None, 'applied': False, 'reason': 'insufficient_voicing'},
              'target_range_status': 'UNKNOWN' if target_range is None else 'EXPLICIT_PROFILE',
              'gender': 'NOT_INFERRED', 'listening_quality': 'READY_FOR_EXTERNAL_VALIDATION'}
    if observations:
        low, median, high = np.percentile(observations, (5, 50, 95))
        report['observed_range_hz'] = dict(low_hz=float(low), median_hz=float(median), high_hz=float(high))
        low_midi, median_midi, high_midi = 69 + 12*np.log2(np.array([low, median, high])/440)
        report['observed_range_midi'] = {'low': float(low_midi), 'median': float(median_midi), 'high': float(high_midi)}
        if source_kind == 'mixed_music':
            report['suggestion']['reason'] = 'mixed_music_is_not_a_vocal_range'
        elif len(observations) < 100 or ratio < .15:
            report['suggestion']['reason'] = 'insufficient_voicing'
        elif target_range is None:
            report['suggestion']['reason'] = 'model_training_range_unknown'
        else:
            if not isinstance(target_range, dict) or not isinstance(target_range.get('provenance'), str) or not target_range['provenance'].strip():
                raise ValueError('Target voice range needs an explicit profile provenance')
            values = [target_range.get(name) for name in ('low_hz', 'median_hz', 'high_hz')]
            if any(type(value) not in (int, float) or not math.isfinite(value) or not 50 <= value <= 2000 for value in values) or not values[0] <= values[1] <= values[2]:
                raise ValueError('Invalid bounded target voice range')
            target_low, target_median, target_high = 69 + 12*np.log2(np.asarray(values)/440)
            def penalty(shift):
                return max(0., target_low-low_midi-shift) + max(0., high_midi+shift-target_high)
            shifts = range(-12, 13)
            best = min(shifts, key=lambda shift: (penalty(shift), abs(shift), abs(median_midi+shift-target_median)))
            report['target_range'] = dict(target_range)
            report['suggestion'] = {'semitones': int(best), 'applied': False,
                                    'reason': 'explicit_profile_range_fit' if best else 'source_already_fits_or_shift_would_not_help',
                                    'range_overflow_before_semitones': float(penalty(0)),
                                    'range_overflow_after_semitones': float(penalty(best))}
    if report_path is not None:
        report_path = os.fspath(report_path)
        if os.path.abspath(path) == os.path.abspath(report_path):
            raise ValueError('Voice observation cannot overwrite source audio')
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        import uuid
        temporary = report_path + '.part.' + uuid.uuid4().hex
        try:
            with open(temporary, 'w', encoding='utf-8') as handle:
                json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
            _check(cancel)
            os.replace(temporary, report_path)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)
    return report
