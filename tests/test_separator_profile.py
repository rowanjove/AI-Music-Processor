import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import enrich, fingerprints, separator_choice_fingerprint_paths
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.separator_routing import (
    dual_profile,
    hp2_profile,
    refuse_unexecuted_route,
    same_source_ab_status,
    voice_separate_binding,
)
from core.storage import stage_cache_key


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
    )


def _model(folder):
    pth = Path(folder) / "model.pth"
    pth.write_bytes(b"weights")
    return {"demo": dict(
        name="demo", pth=str(pth), index=None,
        profile=dict(protect=0.33, rms_mix_rate=0.25, filter_radius=3),
    )}


class SeparatorProfileTests(unittest.TestCase):
    def test_new_jobs_freeze_dual_and_old_snapshot_stays_on_hp2(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("core.job_spec.get_available_rvc_models", return_value=_model(folder)):
                spec = freeze(_parameters(), "voice_conversion")
                legacy = copy.deepcopy(spec)
                legacy.pop("separator_profile", None)
                legacy.pop("effective_separator", None)
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
                chosen = dict(_parameters())
                chosen["separator_profile"] = dual_profile()
                dual = freeze(chosen, "voice_conversion")
                explicit_hp2 = freeze(dict(_parameters(), separator_profile=hp2_profile()), "voice_conversion")
                tampered = copy.deepcopy(dual)
                tampered["separator_profile"]["vocal"]["parameters"] = {"agg": 10}
                tampered["spec_hash"] = digest(tampered)
                with self.assertRaises(CorruptJobError):
                    validate(tampered, "voice_conversion")
                with self.assertRaises(ValueError):
                    freeze(dict(_parameters(), separator_profile=tampered["separator_profile"]), "voice_conversion")
        self.assertEqual(spec["schema_version"], 1)
        self.assertEqual(spec["separator_backend"], "hp2")
        self.assertEqual(spec["separator_profile"]["mode"], "dual")
        self.assertEqual(set(spec["effective_separator"]), {"vocal", "instrumental"})
        self.assertNotIn("separator_profile", restored)
        self.assertEqual(dual["schema_version"], 1)
        self.assertEqual(dual["separator_backend"], "hp2")
        self.assertEqual(dual["separator_profile"]["mode"], "dual")
        self.assertEqual(dual["separator_profile"]["vocal"]["keeps"], "vocal")
        self.assertEqual(dual["separator_profile"]["instrumental"]["keeps"], "instrumental")
        self.assertEqual(dual["separator_profile"]["vocal"]["parameters"], {})
        self.assertEqual(dual["separator_profile"]["instrumental"]["parameters"], {})
        self.assertEqual(dual["separator_profile"]["vocal"]["component_id"], "engine.separator.kim_vocal_2")
        self.assertEqual(dual["separator_profile"]["instrumental"]["component_id"], "engine.separator.inst_hq_3")
        self.assertEqual(explicit_hp2["separator_profile"]["mode"], "hp2")
        self.assertNotIn("effective_separator", explicit_hp2)
        binding = voice_separate_binding(restored.get("separator_profile"), "voice_conversion")
        self.assertEqual(binding["execution"], "single_pass")
        self.assertEqual(binding["parameters"], {"model": "four-stem-addon-or-HP2", "mode": "voice_conversion"})
        dual_binding = voice_separate_binding(dual["separator_profile"], "voice_conversion")
        self.assertEqual(dual_binding["execution"], "split")
        self.assertNotIn("agg", dual_binding["parameters"]["separator_profile"]["vocal"]["parameters"])
        with self.assertRaises(RuntimeError) as caught:
            refuse_unexecuted_route(dual_binding)
        self.assertIn("不会改跑 HP2", str(caught.exception))

    def test_only_a_dual_choice_changes_the_separator_fingerprint(self):
        hp2_parameters = {"model": "four-stem-addon-or-HP2", "mode": "voice_conversion"}
        dual_parameters = voice_separate_binding(dual_profile(), "voice_conversion")["parameters"]
        self.assertEqual(separator_choice_fingerprint_paths(ROOT, {"separator_backend": "hp2"}), [])
        self.assertEqual(separator_choice_fingerprint_paths(ROOT, hp2_parameters), [])
        dual_paths = separator_choice_fingerprint_paths(ROOT, dual_parameters)
        self.assertEqual(dual_paths, [ROOT / "core" / "separator_routing.py"])
        hp2_files = fingerprints("Separate", hp2_parameters)
        dual_files = fingerprints("Separate", dual_parameters)
        self.assertTrue(any(path.endswith("HP2_all_vocals.pth") for path in hp2_files))
        self.assertFalse(any(path.endswith("separator_routing.py") for path in hp2_files))
        self.assertFalse(any(path.endswith("HP2_all_vocals.pth") for path in dual_files))
        self.assertTrue(any(path.endswith("separator_routing.py") for path in dual_files))
        self.assertTrue(any(path.endswith("Kim_Vocal_2.onnx") for path in dual_files))
        self.assertIn("component:engine.separator.kim_vocal_2", dual_files)
        self.assertIn("component:engine.separator.inst_hq_3", dual_files)
        self.assertNotIn("component:engine.separator.kim_vocal_2", hp2_files)
        hp2_key = stage_cache_key("Separate", {"input": "same"}, enrich("Separate", hp2_parameters))
        dual_key = stage_cache_key("Separate", {"input": "same"}, enrich("Separate", dual_parameters))
        self.assertNotEqual(hp2_key, dual_key)
        self.assertEqual(enrich("Separate", hp2_parameters)["stage_contract_version"], 2)
        self.assertEqual(enrich("Separate", dual_parameters)["stage_contract_version"], 2)
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("self._separate_dual_and_copy(", pipeline)
        self.assertIn('"model": "four-stem-addon-or-HP2"', pipeline)
        self.assertIn("voice_separate_binding", pipeline)
        self.assertIn("refuse_unexecuted_route", pipeline)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))

    def test_same_source_comparison_is_not_a_quality_pass(self):
        status = same_source_ab_status()
        self.assertEqual(status["status"], "READY_FOR_EXTERNAL_VALIDATION")
        self.assertFalse(status["ran"])
        self.assertIsNone(status["peaks"])
        self.assertEqual(status["listening_quality"], "not_assessed")
        self.assertEqual(status["quality_acceptance"], "NOT_ASSESSED")
        self.assertTrue(status["loader_implemented"])
        self.assertTrue(all(status["weight_present"].values()))


if __name__ == "__main__":
    unittest.main()
