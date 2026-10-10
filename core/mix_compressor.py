"""Choose whether Mix keeps today's fixed vocal compressor.

Missing or false keeps acompressor=threshold=0.125:ratio=2.5:attack=15:release=80.
True removes that filter only. Echo, gains, and the -3 dB mix trim stay put.
This is not a new compressor curve and not a listening recommendation.
"""

COMPRESSOR = "acompressor=threshold=0.125:ratio=2.5:attack=15:release=80"


def validate_skip_mix_compressor(value):
    if type(value) is not bool:
        raise ValueError("skip_mix_compressor 必须是布尔值")
    return value


def resolve_skip_mix_compressor(parameters):
    """Missing means today's compressor still runs."""
    if not isinstance(parameters, dict) or "skip_mix_compressor" not in parameters:
        return False
    return validate_skip_mix_compressor(parameters["skip_mix_compressor"])
