"""Execution-only reference policy; immutable identity failures never bypass."""
import copy
import math
import os
import subprocess

from core.storage import sha256_file


def prepare_reference(policy, identity, decode=None):
    effective = copy.deepcopy(policy)
    if not effective.get('reference', {}).get('enabled'):
        return effective, None, None
    if not isinstance(identity, dict):
        raise ValueError('Reference Match lacks a frozen identity')
    path, expected = identity.get('path'), identity.get('sha256')
    duration = identity.get('duration')
    fingerprint = identity.get('analysis_fingerprint')
    if (not isinstance(path, str) or not os.path.isfile(path) or os.path.islink(path)
            or not isinstance(expected, str) or len(expected) != 64
            or not isinstance(fingerprint, str) or not fingerprint
            or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
        raise ValueError('Reference Match frozen identity is invalid or unavailable')
    if sha256_file(path) != expected:
        raise ValueError('Reference Match frozen source changed')
    from core.polish_policy import decode_reference
    from core.polish_analysis import spectrum_fingerprint
    try:
        audio, rate = (decode or decode_reference)(path, expected_sha256=expected)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        # Cancellation, exhausted resources and identity/non-finite ValueErrors
        # deliberately do not enter this decoder/read-failure path.
        from core.processes import ProcessCancelled
        if isinstance(exc, ProcessCancelled):
            raise
        if not os.path.isfile(path) or sha256_file(path) != expected:
            raise ValueError('Reference Match source changed during failed decoding') from exc
        effective['reference']['enabled'] = False
        effective['reference']['read_error'] = '参考曲读取失败，Reference Match 已旁路：' + type(exc).__name__
        return effective, None, None
    if sha256_file(path) != expected:
        raise ValueError('Reference Match source changed during decoding')
    if spectrum_fingerprint(audio, rate) != fingerprint or abs(len(audio) / rate - duration) > 1.0 / rate:
        raise ValueError('Reference Match decoded analysis identity changed')
    return effective, audio, rate
