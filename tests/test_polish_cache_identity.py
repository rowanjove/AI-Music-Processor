"""Regression coverage for profile and analysis-sensitive polish caches."""
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.polish_policy import (default_policy, repair_parameters, tone_parameters,
                                dynamics_parameters, stereo_parameters)
from core.storage import stage_cache_key


class PolishCacheIdentityTests(unittest.TestCase):
    def test_strength_profiles_have_distinct_stage_keys(self):
        for stage, parameters in (("Repair", repair_parameters), ("Tone", tone_parameters),
                                  ("Dynamics", dynamics_parameters), ("Stereo", stereo_parameters)):
            with self.subTest(stage=stage):
                keys = {stage_cache_key(stage, "same_audio", parameters(default_policy(profile)))
                        for profile in ("gentle", "balanced", "strong")}
                self.assertEqual(len(keys), 3)

    def test_profile_does_not_leak_reference_into_repair(self):
        first = default_policy("balanced")
        second = default_policy("balanced")
        second["reference"].update(path="reference.wav", sha256="changed")
        self.assertEqual(repair_parameters(first), repair_parameters(second))
        self.assertNotEqual(tone_parameters(first), tone_parameters(second))

    def test_original_analysis_changes_key_after_bypass(self):
        inputs = {"input": {"sha256": "same_wav"}, "analysis": {"sha256": "analysis_v1"}}
        changed = {"input": inputs["input"], "analysis": {"sha256": "analysis_v2"}}
        policy = default_policy("balanced")
        self.assertNotEqual(stage_cache_key("Tone", inputs, tone_parameters(policy)),
                            stage_cache_key("Tone", changed, tone_parameters(policy)))


if __name__ == "__main__":
    unittest.main()
