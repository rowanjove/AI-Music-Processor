"""Paths the cache cleaner must not delete.

Finished audio, stems, MIDI, and user or managed voice weights stay even if
a caller hands them to a cleanup plan. Regenerable files live under .work.
"""
import os

_BLOCKED = ("stems", "midi", "master", "models")


def cleanup_forbidden(path):
    """True when a cleanup pass must leave this path alone."""
    text = os.path.normcase(os.path.abspath(os.fspath(path)))
    parts = text.split(os.sep)
    if ".work" not in parts:
        return True
    for name in _BLOCKED:
        if name in parts:
            return True
    return False
