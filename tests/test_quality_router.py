import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.quality_router import route_quality


FACTS = {
    "sample_rate": 48000,
    "sample_peak": 0.85,
    "clipped_samples": 0,
    "channels": 2,
}


class QualityRouterTests(unittest.TestCase):
    def test_router_keeps_the_current_defaults(self):
        suno = route_quality("suno", FACTS, "balanced")
        studio = route_quality("studio_master", FACTS, "balanced")
        self.assertFalse(suno["applied"])
        self.assertFalse(suno["changes_job"])
        self.assertEqual(suno["separator"], studio["separator"])
        self.assertEqual(suno["separator"]["vocal"], "hp2")
        self.assertEqual(suno["separator"]["instrumental"], "hp2")
        self.assertEqual(suno["separator"]["execution"], "single_pass")
        self.assertEqual(suno["vocal_restore"], [])
        self.assertEqual(suno["f0_method"], "harvest")
        self.assertEqual(suno["rmvpe"], "NOT_RUN")
        self.assertFalse(suno["pitch_applied"])
        self.assertEqual(suno["pre_separator_clean"], "none")
        self.assertEqual(suno["preprocess_recommendation"], "opt_in_disabled")
        self.assertEqual(suno["preprocess_reasons"], [])
        self.assertEqual(suno["retry_budget"], 0)
        self.assertEqual(suno["search_budget"], 3)
        self.assertFalse(suno["search_executed"])
        self.assertEqual(suno["hardware"], "NOT_SUPPLIED")
        self.assertTrue(suno["values_are_current_defaults"])
        self.assertEqual(suno["profile_values"]["index_rate"], 0.65)
        self.assertEqual(suno["profile_values"]["protect"], 0.33)
        self.assertEqual(suno["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(route_quality("auto", FACTS, "fast")["search_budget"], 1)
        self.assertEqual(route_quality("auto", FACTS, "expert", expert_limit=2)["search_budget"], 2)
        with self.assertRaises(ValueError):
            route_quality("auto", FACTS, "magic")
        clipped = dict(FACTS, clipped_samples=2, sample_peak=1.0)
        reviewed = route_quality("suno", clipped, "balanced")
        self.assertEqual(reviewed["pre_separator_clean"], "none")
        self.assertEqual(reviewed["preprocess_recommendation"], "review_clipping_no_edit")
        self.assertIn("存在削波或满幅", reviewed["preprocess_reasons"])
        self.assertFalse(reviewed["applied"])
        self.assertEqual(reviewed["separator"]["vocal"], "hp2")
        self.assertEqual(reviewed["vocal_restore"], [])
        with self.assertRaises(ValueError):
            route_quality("if_suno", FACTS)
        with self.assertRaises(ValueError):
            route_quality("auto", ["not-facts"])

    def test_runtime_plan_uses_the_frozen_spec_and_completed_stage_evidence(self):
        from core.separator_routing import dual_profile

        profile = dual_profile()
        spec = {
            "spec_hash": "frozen-hash",
            "source_hint": "suno",
            "source_preclean_enabled": True,
            "separator_profile": profile,
            "effective_separator": {"vocal": {"sha256": "kim"}, "instrumental": {"sha256": "inst"}},
            "f0_method": "rmvpe",
            "effective_f0": {"requested": "rmvpe", "executed": "rmvpe", "fallback": False},
            "parameter_search_enabled": True,
            "parameter_search_budget": "balanced",
            "quality_retry_limit": 1,
            "effective_rvc": {"profile": {"index_rate": .42, "protect": .2, "rms_mix_rate": .3, "filter_radius": 2}},
            "effective_vocal_restoration": {"effect_id": "de_echo_normal"},
        }
        stages = [
            {"stage_name": "Separate", "status": "DONE", "parameters": {"separator_profile": profile}},
            {"stage_name": "SourceClean", "status": "DONE", "parameters": {"source_preclean_enabled": True}},
            {"stage_name": "RVC", "status": "DONE", "parameters": {
                "f0_requested": "rmvpe", "f0_method": "harvest",
                "rvc_input_headroom_strategy": "auto_peak", "rvc_input_gain_db": -2.5,
                "rvc_input_peak_dbfs": -3.5,
            }, "outputs": {"headroom_report": "song.headroom.json"}},
            {"stage_name": "VocalRestore", "status": "FAILED", "parameters": {"effect_id": "de_echo_normal"}},
            {"stage_name": "RVCSearch", "status": "DONE", "parameters": {"parameter_search_budget": "balanced"}},
        ]
        record = route_quality(job_spec=spec, executed_stages=stages)
        self.assertEqual(record["frozen_spec_hash"], "frozen-hash")
        self.assertEqual(record["separator"]["vocal"], "kim_vocal_2")
        self.assertEqual(record["separator"]["instrumental"], "inst_hq_3")
        self.assertEqual(record["separator"]["stage_status"], "DONE")
        self.assertEqual(record["f0_method_planned"], "rmvpe")
        self.assertEqual(record["f0_method"], "harvest")
        self.assertEqual(record["rmvpe"], "FALLBACK_TO_HARVEST")
        self.assertEqual(record["vocal_restore"], [])
        self.assertEqual(record["vocal_restore_selected"], "de_echo_normal")
        self.assertTrue(record["search_executed"])
        self.assertEqual(record["search_budget"], 3)
        self.assertEqual(record["rvc_input_headroom"]["gain_db"], -2.5)
        self.assertEqual(record["rvc_input_headroom"]["report_path"], "song.headroom.json")
        self.assertEqual(record["profile_values"]["index_rate"], .42)
        self.assertEqual(record["quality_acceptance"], "NOT_ASSESSED")
