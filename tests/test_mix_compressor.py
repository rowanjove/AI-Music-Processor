import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.analyzer import probe_metadata
from core.cache_fingerprint import enrich, mix_compressor_fingerprint_paths
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.mix_compressor import (
    COMPRESSOR,
    resolve_skip_mix_compressor,
    validate_skip_mix_compressor,
)
from core.mixer import mix_stems
from core.qc import sample_metrics

RECORD = ROOT / "tests" / "fixtures" / "voice_chain_vcq007_compressor.json"
ECHO_AFTER = ROOT / "tests" / "fixtures" / "voice_chain_vcq006_after.json"
SHORT_PROJECT = "output/acceptance/audit_repair/验收 中文短样本_20261004_192142_144337"
ECHO = "aecho=1:1:20:0.07"


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
    )


def _filter_complex(add_reverb=None, skip_mix_compressor=None):
    kwargs = {}
    if add_reverb is not None:
        kwargs["add_reverb"] = add_reverb
    if skip_mix_compressor is not None:
        kwargs["skip_mix_compressor"] = skip_mix_compressor
    with mock.patch("core.mixer.measure_loudness_ebur128", return_value={"integrated_lufs": -12.0}), \
         mock.patch("core.mixer.probe_metadata", return_value={"duration": 1.0}), \
         mock.patch("core.mixer.run") as run:
        mix_stems("vocal.wav", "inst.wav", "mix.wav", **kwargs)
    command = run.call_args.args[0]
    return command[command.index("-filter_complex") + 1]


def _level(path):
    metrics = sample_metrics(path)
    peak = metrics["sample_peak"]
    rms = metrics["channel_rms"]
    return {
        "sample_peak": peak,
        "clipped_samples": metrics["clipped_samples"],
        "channel_rms": rms,
        "duration_seconds": probe_metadata(path)["duration"],
        "crest_factor": [peak / value for value in rms],
    }


def measure_compressor():
    """Remix one short project with echo off. Only the compressor changes."""
    project = ROOT / SHORT_PROJECT
    stems = project / "stems"
    vocal = str(stems / "03_vocal_aligned.wav")
    instrumental = str(stems / "04_instrumental.wav")
    with tempfile.TemporaryDirectory(prefix="vcq007-") as folder:
        kept_path = os.path.join(folder, "06_mixed_compressor.wav")
        skipped_path = os.path.join(folder, "06_mixed_no_compressor.wav")
        mix_stems(vocal, instrumental, kept_path, vocal_gain_db=0.5, inst_gain_db=-1.0, add_reverb=False)
        mix_stems(
            vocal, instrumental, skipped_path,
            vocal_gain_db=0.5, inst_gain_db=-1.0,
            add_reverb=False, skip_mix_compressor=True,
        )
        kept = _level(kept_path)
        skipped = _level(skipped_path)
    return {
        "role": "comparison",
        "change": "VCQ-007",
        "listening_quality": "not_assessed",
        "echo": "off",
        "f0": "not_rerun",
        "separator": "not_rerun",
        "compressor": COMPRESSOR,
        "crest_definition": "sample_peak divided by each channel_rms",
        "project_relative": SHORT_PROJECT,
        "kept_compressor": kept,
        "skipped_compressor": skipped,
    }


class MixCompressorTests(unittest.TestCase):
    def test_skip_removes_only_compressor(self):
        self.assertEqual(COMPRESSOR, "acompressor=threshold=0.125:ratio=2.5:attack=15:release=80")
        self.assertFalse(resolve_skip_mix_compressor({}))
        self.assertFalse(resolve_skip_mix_compressor({"skip_mix_compressor": False}))
        self.assertTrue(resolve_skip_mix_compressor({"skip_mix_compressor": True}))
        for value in (1, 0, "true", None):
            with self.assertRaises(ValueError):
                validate_skip_mix_compressor(value)
        default_filter = _filter_complex()
        skipped_filter = _filter_complex(skip_mix_compressor=True)
        echo_filter = _filter_complex(add_reverb=True)
        echo_skipped_filter = _filter_complex(add_reverb=True, skip_mix_compressor=True)
        self.assertNotIn("aecho", default_filter)
        self.assertIn(COMPRESSOR, default_filter)
        self.assertNotIn("aecho", skipped_filter)
        self.assertNotIn("acompressor", skipped_filter)
        self.assertIn(ECHO, echo_filter)
        self.assertIn(COMPRESSOR, echo_filter)
        self.assertIn(ECHO, echo_skipped_filter)
        self.assertNotIn("acompressor", echo_skipped_filter)
        with self.assertRaises(ValueError):
            _filter_complex(skip_mix_compressor=1)
        mixer = (ROOT / "core" / "mixer.py").read_text(encoding="utf-8")
        self.assertEqual(mixer.count(COMPRESSOR), 1)
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("skip_compressor = resolve_skip_mix_compressor(params)", pipeline)
        self.assertIn('mix_parameters["skip_mix_compressor"] = True', pipeline)
        self.assertIn("add_reverb=add_mix_reverb, skip_mix_compressor=skip_compressor", pipeline)
        self.assertNotIn("add_reverb=True", pipeline)
        plain = enrich("Mix", {"reverb": False})
        marked = enrich("Mix", {"reverb": False, "skip_mix_compressor": True})
        self.assertFalse(any("mix_compressor.py" in key for key in plain["dependency_hashes"]))
        self.assertTrue(any("mixer.py" in key for key in plain["dependency_hashes"]))
        self.assertTrue(any("mix_compressor.py" in key for key in marked["dependency_hashes"]))
        self.assertEqual(mix_compressor_fingerprint_paths(ROOT, {"reverb": False}), [])
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", fingerprint)

    def test_short_sample_crest_and_legacy_snapshot(self):
        project = ROOT / SHORT_PROJECT
        self.assertTrue(project.is_dir(), "短样本工程不在磁盘上")
        historical_mix = project / ".work" / "06_mixed.wav"
        before_bytes = historical_mix.read_bytes()
        record = measure_compressor()
        self.assertEqual(historical_mix.read_bytes(), before_bytes)
        kept = record["kept_compressor"]
        skipped = record["skipped_compressor"]
        echo_after = json.loads(ECHO_AFTER.read_text(encoding="utf-8"))["points"]["mix_output"]
        for name in ("sample_peak", "channel_rms", "clipped_samples", "duration_seconds"):
            self.assertEqual(kept[name], echo_after[name])
        self.assertEqual(kept["clipped_samples"], 0)
        self.assertEqual(skipped["clipped_samples"], 0)
        self.assertEqual(kept["duration_seconds"], skipped["duration_seconds"])
        self.assertNotEqual(kept["sample_peak"], skipped["sample_peak"])
        self.assertNotEqual(kept["crest_factor"], skipped["crest_factor"])
        self.assertEqual(record["echo"], "off")
        self.assertEqual(record["f0"], "not_rerun")
        self.assertEqual(record["separator"], "not_rerun")
        self.assertEqual(record["listening_quality"], "not_assessed")
        fixture = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(record, fixture)
        text = RECORD.read_text(encoding="utf-8")
        self.assertNotIn("听感变好", text)
        self.assertNotIn("improved", text)
        with tempfile.TemporaryDirectory() as folder:
            pth = Path(folder) / "model.pth"
            pth.write_bytes(b"weights")
            model = dict(
                name="demo", pth=str(pth), index=None,
                profile=dict(protect=0.33, rms_mix_rate=0.25, filter_radius=3),
            )
            with patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
                spec = freeze(_parameters(), "voice_conversion")
                polish = freeze(_parameters(), "polish")
                explicit = _parameters()
                explicit["skip_mix_compressor"] = True
                chosen = freeze(explicit, "voice_conversion")
                legacy = copy.deepcopy(spec)
                del legacy["skip_mix_compressor"]
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
                bad = copy.deepcopy(spec)
                bad["skip_mix_compressor"] = "yes"
                bad["spec_hash"] = digest(bad)
                with self.assertRaises(CorruptJobError):
                    validate(bad, "voice_conversion")
        self.assertEqual(spec["schema_version"], 1)
        self.assertFalse(spec["skip_mix_compressor"])
        self.assertNotIn("skip_mix_compressor", polish)
        self.assertTrue(chosen["skip_mix_compressor"])
        self.assertNotIn("skip_mix_compressor", restored)
        self.assertFalse(resolve_skip_mix_compressor(restored))
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))
