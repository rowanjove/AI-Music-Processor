"""Current-stage estimates from matching actual completed stage timings."""
import datetime
import itertools
import math
import statistics

ALGORITHM_VERSION = 'observed-stage-wallclock-v1'


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _timestamp(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def estimate_remaining(history, current_duration, mode, stage, identity=None,
                       elapsed_seconds=0, stage_fraction=None):
    """Estimate this stage, not an unmeasured sum for the entire job.

History rows require mode, stage_name, audio_duration, started_at, finished_at,
execution_kind='executed', and exact identity (backend/hardware plus caller's
model/runtime/profile hashes). Cached, reused and unknown execution are ignored.
"""
    if not _positive(current_duration) or type(elapsed_seconds) not in (int, float) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError('Invalid finite current duration/elapsed time')
    if stage_fraction is not None and (type(stage_fraction) not in (int, float) or not math.isfinite(stage_fraction) or not 0 <= stage_fraction <= 1):
        raise ValueError('Invalid observed stage fraction')
    result = {'algorithm': ALGORITHM_VERSION, 'scope': 'current_stage', 'seconds': None,
              'range_seconds': None, 'basis': 'insufficient_observed_history', 'sample_count': 0,
              'elapsed_seconds': float(elapsed_seconds), 'observed_fraction': stage_fraction,
              'timing_kind': 'stage_wallclock', 'range_kind': 'empirical_with_20_percent_margin'}
    if not isinstance(identity, dict) or any(not isinstance(identity.get(key), str) or not identity[key] for key in ('backend', 'hardware')):
        result['basis'] = 'execution_identity_unknown'
        return result
    predictions = []
    for row in itertools.islice(history, 2000):
        if not isinstance(row, dict) or row.get('status') != 'DONE' or row.get('mode') != mode or row.get('stage_name') != stage:
            continue
        if row.get('identity') != identity or str(row.get('execution_kind', '')).lower() != 'executed' or row.get('cache_hit') or row.get('reused'):
            continue
        duration = row.get('audio_duration')
        if not _positive(duration) or not .5 <= duration/current_duration <= 2:
            continue
        started, finished = _timestamp(row.get('started_at')), _timestamp(row.get('finished_at'))
        if started is None or finished is None or finished <= started:
            continue
        paused = row.get('paused_seconds', 0)
        if type(paused) not in (int, float) or not math.isfinite(paused) or paused < 0 or (row.get('paused') and 'paused_seconds' not in row):
            continue
        elapsed = finished-started-paused
        if not _positive(elapsed):
            continue
        predictions.append(elapsed * current_duration/duration)
    result['sample_count'] = len(predictions)
    if len(predictions) < 2:
        return result
    median = statistics.median(predictions)
    ordered = sorted(predictions)
    lower, upper = ordered[int((len(ordered)-1)*.25)], ordered[math.ceil((len(ordered)-1)*.75)]
    margin = max(1., median*.2)
    result['range_seconds'] = [max(0., lower-margin-elapsed_seconds), max(0., upper+margin-elapsed_seconds)]
    if elapsed_seconds >= median:
        result['basis'] = 'running_longer_than_observed_median'
        return result
    result['seconds'] = float(median-elapsed_seconds)
    result['basis'] = 'matching_actual_stage_history'
    return result
