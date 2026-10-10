import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import enrich
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.qc import sample_metrics
from core.rvc_postprocess import (
    POSTPROCESS_GAIN,
    apply_rvc_postprocess,
    prepare_align_input,
    resolve_rvc_postprocess,
    validate_rvc_postprocess,
)

RECORD = ROOT / "tests" / "fixtures" / "voice_chain_vcq005_postprocess.json"


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
    )


def _level(path):
    metrics = sample_metrics(path)
    return {
        "sample_peak": metrics["sample_peak"],
        "clipped_samples": metrics["clipped_samples"],
        "channel_rms": metrics["channel_rms"],
    }


def measure_postprocess():
    """Compare an untouched RVC file with the opt-in half-scale file."""
    samples = np.full((4800, 2), np.float32(0.4))
    with tempfile.TemporaryDirectory(prefix="rvc-post-") as folder:
        source = str(Path(folder) / "03_vocal_rvc_fixture.wav")
        dest = str(Path(folder) / "07_rvc_post.wav")
        sf.write(source, samples, 48000, subtype="FLOAT")
        before = Path(source).read_bytes()
        chosen = prepare_align_input(source, dest, False)
        empty_same_path = chosen == source
        empty_same_bytes = Path(source).read_bytes() == before
        empty_wrote_dest = Path(dest).exists()
        enabled = prepare_align_input(source, dest, True)
        source_after = Path(source).read_bytes()
        return {
            "role": "comparison",
            "change": "VCQ-005",
            "listening_quality": "not_assessed",
            "signal": "4800 stereo samples at float32 0.4, 48000 Hz",
            "gain": POSTPROCESS_GAIN,
            "empty_same_path": empty_same_path,
            "empty_same_bytes": empty_same_bytes,
            "empty_wrote_dest": empty_wrote_dest,
            "source_bytes_unchanged_after_enable": source_after == before,
            "enabled_file_differs": Path(enabled).read_bytes() != before,
            "source": _level(source),
            "enabled": _level(enabled),
        }


class RvcPostprocessTests(unittest.TestCase):
    def test_empty_matches_bytes_and_enabled_changes_level(self):
        self.assertEqual(POSTPROCESS_GAIN, 0.5)
        self.assertFalse(resolve_rvc_postprocess({}))
        self.assertFalse(resolve_rvc_postprocess({"rvc_postprocess": False}))
        self.assertTrue(resolve_rvc_postprocess({"rvc_postprocess": True}))
        for value in (1, 0, "true", None):
            with self.assertRaises(ValueError):
                validate_rvc_postprocess(value)
        record = measure_postprocess()
        self.assertTrue(record["empty_same_path"])
        self.assertTrue(record["empty_same_bytes"])
        self.assertFalse(record["empty_wrote_dest"])
        self.assertTrue(record["source_bytes_unchanged_after_enable"])
        self.assertTrue(record["enabled_file_differs"])
        self.assertEqual(record["source"]["clipped_samples"], 0)
        self.assertEqual(record["enabled"]["clipped_samples"], 0)
        self.assertAlmostEqual(record["enabled"]["sample_peak"], record["source"]["sample_peak"] * 0.5, places=6)
        self.assertAlmostEqual(record["enabled"]["channel_rms"][0], record["source"]["channel_rms"][0] * 0.5, places=6)
        fixture = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(record, fixture)
        with tempfile.TemporaryDirectory() as folder:
            source = str(Path(folder) / "raw.wav")
            sf.write(source, np.full(16, np.float32(0.2)), 48000, subtype="FLOAT")
            with self.assertRaises(ValueError):
                apply_rvc_postprocess(source, source)

    def test_legacy_snapshot_stays_empty_and_slot_is_before_align(self):
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
                explicit["rvc_postprocess"] = True
                chosen = freeze(explicit, "voice_conversion")
                legacy = copy.deepcopy(spec)
                del legacy["rvc_postprocess"]
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
                bad = copy.deepcopy(spec)
                bad["rvc_postprocess"] = "yes"
                bad["spec_hash"] = digest(bad)
                with self.assertRaises(CorruptJobError):
                    validate(bad, "voice_conversion")
        self.assertEqual(spec["schema_version"], 1)
        self.assertFalse(spec["rvc_postprocess"])
        self.assertNotIn("rvc_postprocess", polish)
        self.assertTrue(chosen["rvc_postprocess"])
        self.assertNotIn("rvc_postprocess", restored)
        self.assertFalse(resolve_rvc_postprocess(restored))
        posted = enrich("RVCPost", {"rvc_postprocess": False})
        clean = enrich("Clean", {"preset": "light", "vocal": True})
        self.assertTrue(any("rvc_postprocess.py" in key for key in posted["dependency_hashes"]))
        self.assertFalse(any("rvc_postprocess.py" in key for key in clean["dependency_hashes"]))
        module = (ROOT / "core" / "rvc_postprocess.py").read_text(encoding="utf-8")
        self.assertNotIn("aecho", module)
        self.assertNotIn("acompressor", module)
        self.assertNotIn("mix_stems", module)
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertLess(pipeline.index("inspect_rvc_raw"), pipeline.index("prepare_align_input"))
        self.assertLess(pipeline.index("prepare_align_input"), pipeline.index("self._align"))
        self.assertIn('07_rvc_post.wav', pipeline)
        self.assertIn('{"rvc_postprocess": post_enabled}', pipeline)
        self.assertIn("align_input", pipeline)
        self.assertIn("'RVCPost'", pipeline)
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", fingerprint)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))
