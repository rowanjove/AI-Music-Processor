"""Suggest a pitch shift from a model's own gender hints.

The number is not a processing input. Callers keep the pitch the user
submitted. Unknown models do not receive a blanket 12 semitone shift.
"""

_SOURCE_FIELDS = {"male": "default_pitch_male", "female": "default_pitch_female"}


def suggest_pitch(model, source_gender):
    """Return semitones and a short label. ``applied`` stays false."""
    if not isinstance(model, dict) or model.get("gender") not in _SOURCE_FIELDS:
        return _none("模型没有性别提示")
    field = _SOURCE_FIELDS.get(source_gender)
    if field is None:
        return _none("没有输入性别")
    value = model.get(field)
    if type(value) is not int or not -24 <= value <= 24:
        return _none("音高提示无效")
    return {
        "semitones": value,
        "text": "建议 %d 半音" % value,
        "applied": False,
        "reason": "使用模型的 %s" % field,
    }


def _none(reason):
    return {"semitones": 0, "text": "无建议", "applied": False, "reason": reason}
