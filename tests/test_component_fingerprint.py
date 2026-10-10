import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import enrich
from core.component_identity import declaration_sha256
from core.storage import stage_cache_key

RECORD = ROOT / "tests" / "fixtures" / "cache_fingerprint_found011.json"
INPUT = {"input": "found-011"}
OBSERVATIONS = (
    ("Ingest", "Ingest", {"mode": "voice_conversion"}),
    ("Analyze", "Analyze", {}),
    ("Clean", "Clean", {"preset": "light"}),
    ("Separate:voice_conversion", "Separate", {"mode": "voice_conversion"}),
    ("Separate:transcribe", "Separate", {"mode": "transcribe"}),
    ("RVC", "RVC", {"effective_rvc": {}}),
    ("RVCPost", "RVCPost", {"rvc_postprocess": False}),
    ("Align", "Align", {}),
    ("Mix", "Mix", {"reverb": False}),
    ("Master", "Master", {"preset": "natural"}),
    ("QC", "QC", {}),
    ("Transcribe", "Transcribe", {"bpm": 120}),
)
CASES = (
    ("engine.rvc", "RVC"),
    ("model.hubert", "RVC"),
    ("engine.separator.hp2", "Separate:voice_conversion"),
    ("engine.separator.htdemucs", "Separate:transcribe"),
    ("engine.basic_pitch", "Transcribe"),
)


def _key(stage, parameters):
    enriched = enrich(stage, parameters)
    if enriched["stage_contract_version"] != 2:
        raise AssertionError(stage)
    return stage_cache_key(stage, INPUT, enriched), enriched


def measure_fingerprint_cases():
    """Compare cache keys before and after one catalog declaration changes."""
    before = {label: _key(stage, parameters) for label, stage, parameters in OBSERVATIONS}
    cases = []
    for component_id, expected in CASES:
        def wrap(component, target=component_id):
            if component.get("id") == target:
                return "f" * 64
            return declaration_sha256(component)

        with patch("core.component_identity.declaration_sha256", side_effect=wrap):
            changed = []
            unchanged = []
            for label, stage, parameters in OBSERVATIONS:
                key, _enriched = _key(stage, parameters)
                if key == before[label][0]:
                    unchanged.append(label)
                else:
                    changed.append(label)
        cases.append({
            "changed_component": component_id,
            "expected_stage": expected,
            "stages_key_changed": changed,
            "stages_key_unchanged": unchanged,
        })
    clean = before["Clean"][1]["dependency_hashes"]
    rvc = before["RVC"][1]["dependency_hashes"]
    return {
        "change": "FOUND-011",
        "stage_contract_version": 2,
        "clean_has_component_declaration": any(key.startswith("component:") for key in clean),
        "rvc_components": sorted(key for key in rvc if key.startswith("component:")),
        "cases": cases,
    }


class ComponentFingerprintTests(unittest.TestCase):
    def test_one_component_changes_only_its_stage_key(self):
        record = measure_fingerprint_cases()
        self.assertEqual(record["stage_contract_version"], 2)
        self.assertFalse(record["clean_has_component_declaration"])
        self.assertEqual(record["rvc_components"], ["component:engine.rvc", "component:model.hubert"])
        for case in record["cases"]:
            self.assertEqual(case["stages_key_changed"], [case["expected_stage"]])
            self.assertNotIn(case["expected_stage"], case["stages_key_unchanged"])
            self.assertEqual(len(case["stages_key_changed"]) + len(case["stages_key_unchanged"]), len(OBSERVATIONS))
        text = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", text)
        self.assertNotIn("stage_contract_version=3", text)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))
        fixture = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(record, fixture)
        stored = RECORD.read_text(encoding="utf-8")
        self.assertNotIn("improved", stored)
