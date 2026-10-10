"""Decide whether vocal Clean keeps its extra highpass and deesser.

The preset filter stays in place either way. Missing means the historical
path: 75 Hz highpass and deesser still run. True skips only those two extras.
"""


def validate_skip_vocal_eq(value):
    if type(value) is not bool:
        raise ValueError("skip_vocal_eq 必须是布尔值")
    return value


def resolve_skip_vocal_eq(parameters):
    """Missing means today's vocal Clean, not a new default."""
    if not isinstance(parameters, dict) or "skip_vocal_eq" not in parameters:
        return False
    return validate_skip_vocal_eq(parameters["skip_vocal_eq"])


def apply_vocal_extras(mode, parameters):
    """Only voice conversion may skip. Other modes keep the vocal branch."""
    if mode != "voice_conversion":
        return True
    return not resolve_skip_vocal_eq(parameters)
