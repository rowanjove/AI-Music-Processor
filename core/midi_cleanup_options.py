"""Frozen, bounded MIDI cleanup settings shared by GUI and job snapshots."""

DEFAULT_MIDI_CLEANUP = {
    "enabled": False,
    "quantize": False,
    "grid_division": 4,
    "merge_overlaps": False,
    "velocity_range": None,
}


def normalize_midi_cleanup(value=None):
    if value is None:
        return dict(DEFAULT_MIDI_CLEANUP)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_MIDI_CLEANUP):
        raise ValueError("MIDI cleanup settings have unknown fields")
    result = dict(DEFAULT_MIDI_CLEANUP)
    result.update(value)
    for key in ("enabled", "quantize", "merge_overlaps"):
        if type(result[key]) is not bool:
            raise ValueError("MIDI cleanup " + key + " must be boolean")
    if type(result["grid_division"]) is not int or not 1 <= result["grid_division"] <= 32:
        raise ValueError("MIDI cleanup grid_division must be an integer from 1 to 32")
    velocity = result["velocity_range"]
    if velocity is not None:
        if (not isinstance(velocity, (list, tuple)) or len(velocity) != 2 or
                any(type(item) is not int for item in velocity) or
                not 1 <= velocity[0] <= velocity[1] <= 127):
            raise ValueError("MIDI cleanup velocity_range must be two MIDI velocities")
        result["velocity_range"] = list(velocity)
    return result
