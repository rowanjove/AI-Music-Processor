import os
import sys
import tempfile
import unittest
import json
import copy
import ast
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mixer import mix_stems
from core.voice_observation import (
    compare_observations,
    load_observation,
    measure_voice_chain,
    resolve_project_points,
)

BEFORE = ROOT / "tests" / "fixtures" / "voice_chain_before.json"
AFTER = ROOT / "tests" / "fixtures" / "voice_chain_vcq006_after.json"
SHORT_PROJECT = (
    "output/acceptance/audit_repair/验收 中文短样本_20261004_192142_144337"
)
ECHO = "aecho=1:1:20:0.07"
COMPRESSOR = "acompressor=threshold=0.125:ratio=2.5:attack=15:release=80"
METRIC_NAMES = ("duration_seconds", "sample_peak", "channel_rms", "clipped_samples")


def _filter_complex(add_reverb=None):
    kwargs = {}
    if add_reverb is not None:
        kwargs["add_reverb"] = add_reverb
    with mock.patch("core.mixer.measure_loudness_ebur128", return_value={"integrated_lufs": -12.0}), \
         mock.patch("core.mixer.probe_metadata", return_value={"duration": 1.0}), \
         mock.patch("core.mixer.run") as run:
        mix_stems("vocal.wav", "inst.wav", "mix.wav", **kwargs)
    command = run.call_args.args[0]
    return command[command.index("-filter_complex") + 1]


class MixEchoTests(unittest.TestCase):
    def test_default_command_omits_echo_and_explicit_keeps_it(self):
        default_filter = _filter_complex()
        enabled_filter = _filter_complex(True)
        self.assertNotIn("aecho", default_filter)
        self.assertIn(ECHO, enabled_filter)
        self.assertIn(COMPRESSOR, default_filter)
        self.assertIn(COMPRESSOR, enabled_filter)

    def test_pipeline_voice_mix_requests_reverb_off(self):
        from core.job_spec import freeze, validate, digest, resolve_mix_reverb
        from core.cache_fingerprint import enrich
        legacy = json.loads((ROOT / SHORT_PROJECT / 'project.json').read_text(encoding='utf-8'))['job_spec']
        before_hash = legacy['spec_hash']
        restored = validate(copy.deepcopy(legacy), 'voice_conversion')
        self.assertTrue(resolve_mix_reverb(restored))
        self.assertFalse(resolve_mix_reverb(restored, {'reverb': False}))
        self.assertTrue(resolve_mix_reverb(restored, {'reverb': True}))
        self.assertNotIn('mix_reverb', restored)
        self.assertEqual(restored['spec_hash'], before_hash)
        new = freeze(legacy, 'voice_conversion')
        self.assertIs(new['mix_reverb'], False)
        self.assertFalse(resolve_mix_reverb(new))
        self.assertFalse(resolve_mix_reverb(new, {'reverb': True}))
        self.assertNotEqual(new['spec_hash'], before_hash)
        bad = dict(new, mix_reverb='false')
        bad['spec_hash'] = digest(bad)
        with self.assertRaisesRegex(Exception, '布尔'):
            validate(bad, 'voice_conversion')
        self.assertNotEqual(enrich('Mix', {'reverb': True}), enrich('Mix', {'reverb': False}))
        text = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'mix_stems']
        self.assertTrue(any(any(keyword.arg == 'add_reverb' and isinstance(keyword.value, ast.Name)
                               and keyword.value.id == 'add_mix_reverb' for keyword in call.keywords)
                            for call in calls))

    def test_short_chain_mix_after_matches_record(self):
        before = load_observation(str(BEFORE))
        after = load_observation(str(AFTER))
        self.assertEqual(after["role"], "after")
        self.assertEqual(after["change"], "VCQ-006")
        self.assertEqual(after["listening_quality"], "not_assessed")
        self.assertEqual(after["project_relative"], SHORT_PROJECT)
        project = ROOT / SHORT_PROJECT
        self.assertTrue(project.is_dir(), "短样本工程不在磁盘上")
        stems = project / "stems"
        vocal = str(stems / "03_vocal_aligned.wav")
        instrumental = str(stems / "04_instrumental.wav")
        paths = resolve_project_points(str(project))
        with tempfile.TemporaryDirectory(prefix="vcq006-") as folder:
            default_mix = os.path.join(folder, "06_mixed_default.wav")
            explicit_mix = os.path.join(folder, "06_mixed_echo.wav")
            mix_stems(vocal, instrumental, default_mix, vocal_gain_db=0.5, inst_gain_db=-1.0)
            mix_stems(vocal, instrumental, explicit_mix, vocal_gain_db=0.5, inst_gain_db=-1.0, add_reverb=True)
            measured = measure_voice_chain(dict(paths, mix_output=default_mix))
            explicit = measure_voice_chain(dict(paths, mix_output=explicit_mix))
        for key in ("pre_separation", "rvc_output"):
            for name in METRIC_NAMES + ("sha256",):
                self.assertEqual(measured["points"][key][name], before["points"][key][name])
                self.assertEqual(after["points"][key][name], before["points"][key][name])
        for name in METRIC_NAMES:
            self.assertEqual(measured["points"]["mix_output"][name], after["points"]["mix_output"][name])
            self.assertEqual(
                explicit["points"]["mix_output"][name],
                after["context"]["explicit_aecho_mix"][name],
            )
        self.assertNotEqual(
            measured["points"]["mix_output"]["channel_rms"],
            explicit["points"]["mix_output"]["channel_rms"],
        )
        delta = compare_observations(before, after)
        self.assertEqual(delta["listening_quality"], "not_assessed")
        self.assertNotIn("improved", delta)
        self.assertEqual(delta["points"]["pre_separation"]["sample_peak"], 0.0)
        self.assertEqual(delta["points"]["rvc_output"]["sample_peak"], 0.0)
        self.assertNotEqual(delta["points"]["mix_output"]["channel_rms"], [0.0, 0.0])
        text = AFTER.read_text(encoding="utf-8")
        self.assertNotIn("听感变好", text)
        self.assertNotIn("improved", text)


if __name__ == "__main__":
    unittest.main()
