"""Record which golden roles exist. This module does not copy or synthesize audio.

A classified Suno, studio, live, or vocal-only set is not created here.
Listening cannot be marked passed by the caller.
"""
import os

SCHEMA_VERSION = 1
SOURCE_KINDS = ("unclassified", "suno", "studio", "mp3", "live", "vocal_only")
GENDER_PAIRS = ("male_to_male", "male_to_female", "female_to_male", "female_to_female")
SINGING = ("pop", "high", "low", "breathy", "rap", "sustained", "dense_lyrics", "harmony", "reverb")
ROLES = (
    "source",
    "separated_vocal",
    "instrumental",
    "preprocessed_vocal",
    "rvc_input",
    "rvc_raw",
    "postprocess",
    "mix",
    "master",
)


def expected_axes():
    return {
        "schema_version": SCHEMA_VERSION,
        "source_kind": list(SOURCE_KINDS[1:]),
        "gender_pair": list(GENDER_PAIRS),
        "singing": list(SINGING),
        "roles": list(ROLES),
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }


def register_case(case_id, roles, source_kind="unclassified", gender_pair=None, singing=None, listening_passed=False):
    """Register files that already exist. Missing roles stay NOT_PRESENT."""
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("Golden 样本缺少 id")
    if source_kind not in SOURCE_KINDS:
        raise ValueError("未知来源类型")
    if gender_pair is not None and gender_pair not in GENDER_PAIRS:
        raise ValueError("未知性别方向")
    if singing is not None and singing not in SINGING:
        raise ValueError("未知演唱类型")
    if listening_passed:
        raise ValueError("没有听审记录，不能标成通过")
    if not isinstance(roles, dict):
        raise ValueError("Golden 角色必须是字典")
    unknown = sorted(key for key in roles if key not in ROLES)
    if unknown:
        raise ValueError("未知 Golden 角色：" + "、".join(unknown))
    recorded = {}
    for role in ROLES:
        path = roles.get(role)
        if path is None:
            recorded[role] = {"status": "NOT_PRESENT"}
            continue
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ValueError("Golden 角色文件不存在：" + role)
        recorded[role] = {"status": "PRESENT", "bytes": os.path.getsize(path)}
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "source_kind": source_kind,
        "gender_pair": gender_pair or "NOT_ASSESSED",
        "singing": singing or "NOT_ASSESSED",
        "roles": recorded,
        "after_created": False,
        "listening_quality": "not_assessed",
        "quality_acceptance": "NOT_ASSESSED",
    }


def coverage(cases):
    """Count classified cases. An unclassified observation does not fill an axis."""
    if not isinstance(cases, (list, tuple)):
        raise ValueError("样本列表无效")
    filled = {axis: set() for axis in ("source_kind", "gender_pair", "singing")}
    classified = 0
    for case in cases:
        if not isinstance(case, dict) or case.get("quality_acceptance") != "NOT_ASSESSED":
            raise ValueError("样本不能自称为已验收")
        if case.get("source_kind") == "unclassified":
            continue
        classified += 1
        for axis, allowed in (("source_kind", SOURCE_KINDS), ("gender_pair", GENDER_PAIRS), ("singing", SINGING)):
            value = case.get(axis)
            if value in allowed:
                filled[axis].add(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NOT_RUN" if classified == 0 else "PARTIAL_NOT_ACCEPTED",
        "classified_cases": classified,
        "filled": {key: sorted(value) for key, value in filled.items()},
        "listening_passed": 0,
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }
