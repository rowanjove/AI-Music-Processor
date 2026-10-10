"""Pick a few deterministic windows from a long buffer.

A buffer that is not longer than the window is returned unchanged. Longer
audio yields at most `limit` windows, ordered by start time. The same input
returns the same edges. This does not run a model.
"""
WINDOW_SECONDS = 8.0
CLIP_LIMIT = 3


def select_clips(samples, sample_rate, window_seconds=WINDOW_SECONDS, limit=CLIP_LIMIT):
    """Return `samples` itself when it fits. Otherwise return a short clip list."""
    if type(sample_rate) is bool or type(sample_rate) not in (int, float) or sample_rate <= 0:
        raise ValueError("sample_rate 必须为正数")
    if type(window_seconds) is bool or type(window_seconds) not in (int, float) or window_seconds <= 0:
        raise ValueError("窗口时长必须为正数")
    if type(limit) is bool or type(limit) is not int or limit < 1:
        raise ValueError("片段上限至少为 1")
    count = len(samples)
    window = int(window_seconds * float(sample_rate))
    if window < 1:
        window = 1
    if count <= window:
        return samples
    starts = list(range(0, count - window + 1, window))
    ranked = sorted(starts, key=lambda start: (-_energy(samples, start, start + window), start))
    chosen = sorted(ranked[:limit])
    clips = []
    for start in chosen:
        end = start + window
        window_audio = samples[start:end]
        clips.append({
            "start_sample": start,
            "end_sample": end,
            "start_timecode": timecode(start / float(sample_rate)),
            "end_timecode": timecode(end / float(sample_rate)),
            "samples": window_audio,
        })
    return clips


def timecode(seconds):
    """Format seconds as HH:MM:SS.mmm."""
    if seconds < 0:
        seconds = 0.0
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000.0))
    if millis >= 1000:
        whole += 1
        millis -= 1000
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    return "%02d:%02d:%02d.%03d" % (hours, minutes, secs, millis)


def _energy(samples, start, end):
    window = samples[start:end]
    if hasattr(window, "dtype"):
        array = window.astype("float64")
        return float((array * array).sum())
    total = 0.0
    for item in window:
        if isinstance(item, (list, tuple)):
            for channel in item:
                value = float(channel)
                total += value * value
        else:
            value = float(item)
            total += value * value
    return total
