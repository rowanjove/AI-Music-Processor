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

from core.cache_fingerprint import enrich, vocal_eq_fingerprint_paths
from core.cleaner import clean_audio_stream
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.qc import sample_metrics
from core.vocal_eq import apply_vocal_extras, resolve_skip_vocal_eq, validate_skip_vocal_eq

RECORD = ROOT / "tests" / "fixtures" / "voice_chain_vcq004_vocal_eq.json"


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
    )


def _band_mean_square(path, low, high):
    data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    mono = data.mean(axis=1).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(mono))
    freqs = np.fft.rfftfreq(len(mono), 1.0 / sample_rate)
    band = spectrum[(freqs >= low) & (freqs < high)]
    return float(np.mean(band * band))


def _metrics(path):
    metrics = sample_metrics(path)
    return {
        "sample_peak": metrics["sample_peak"],
        "clipped_samples": metrics["clipped_samples"],
        "channel_rms": metrics["channel_rms"],
        "low_band_mean_square": _band_mean_square(path, 40.0, 90.0),
        "high_band_mean_square": _band_mean_square(path, 6000.0, 9000.0),
    }


def measure_vocal_eq_pair():
    """Render one synthetic vocal with today's extras and with the extras skipped."""
    sample_rate = 48000
    count = sample_rate
    time = np.arange(count) / sample_rate
    tone = (0.2 * np.sin(2 * np.pi * 50 * time) + 0.08 * np.sin(2 * np.pi * 7000 * time)).astype(np.float32)
    stereo = np.column_stack((tone, tone))
    with tempfile.TemporaryDirectory(prefix="vocal-eq-") as folder:
        source = str(Path(folder) / "source.wav")
        kept = str(Path(folder) / "kept.wav")
        skipped = str(Path(folder) / "skipped.wav")
        sf.write(source, stereo, sample_rate, subtype="FLOAT")
        clean_audio_stream(source, kept, preset_key="light", vocal=True)
        clean_audio_stream(source, skipped, preset_key="light", vocal=False)
        kept_metrics = _metrics(kept)
        skipped_metrics = _metrics(skipped)
    return {
        "role": "comparison",
        "change": "VCQ-004",
        "listening_quality": "not_assessed",
        "signal": "1 second, 50 Hz at 0.2 plus 7000 Hz at 0.08, stereo",
        "preset": "light",
        "default_vocal_extras": kept_metrics,
        "skipped_vocal_extras": skipped_metrics,
    }


class VocalEqTests(unittest.TestCase):
    def test_missing_keeps_todays_vocal_clean(self):
        self.assertFalse(validate_skip_vocal_eq(False))
        self.assertTrue(validate_skip_vocal_eq(True))
        self.assertFalse(resolve_skip_vocal_eq({}))
        self.assertFalse(resolve_skip_vocal_eq({"skip_vocal_eq": False}))
        self.assertTrue(resolve_skip_vocal_eq({"skip_vocal_eq": True}))
        for value in (1, 0, "true", None):
            with self.assertRaises(ValueError):
                validate_skip_vocal_eq(value)
        self.assertTrue(apply_vocal_extras("voice_conversion", {}))
        self.assertTrue(apply_vocal_extras("transcribe", {"skip_vocal_eq": True}))
        self.assertFalse(apply_vocal_extras("voice_conversion", {"skip_vocal_eq": True}))
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
                explicit["skip_vocal_eq"] = True
                chosen = freeze(explicit, "voice_conversion")
                legacy = copy.deepcopy(spec)
                del legacy["skip_vocal_eq"]
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
                bad = copy.deepcopy(spec)
                bad["skip_vocal_eq"] = 1
                bad["spec_hash"] = digest(bad)
                with self.assertRaises(CorruptJobError):
                    validate(bad, "voice_conversion")
        self.assertEqual(spec["schema_version"], 1)
        self.assertFalse(spec["skip_vocal_eq"])
        self.assertNotIn("skip_vocal_eq", polish)
        self.assertTrue(chosen["skip_vocal_eq"])
        self.assertNotIn("skip_vocal_eq", restored)
        self.assertFalse(resolve_skip_vocal_eq(restored))

    def test_skip_changes_low_band_and_default_cache_identity(self):
        record = measure_vocal_eq_pair()
        default = record["default_vocal_extras"]
        skipped = record["skipped_vocal_extras"]
        self.assertEqual(default["clipped_samples"], 0)
        self.assertEqual(skipped["clipped_samples"], 0)
        self.assertGreater(default["sample_peak"], 1e-4)
        self.assertGreater(skipped["sample_peak"], 1e-4)
        self.assertGreater(skipped["low_band_mean_square"], default["low_band_mean_square"])
        fixture = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(record, fixture)
        root = ROOT
        self.assertEqual(vocal_eq_fingerprint_paths(root, {"vocal": True}), [])
        self.assertEqual(vocal_eq_fingerprint_paths(root, {"skip_vocal_eq": False}), [])
        self.assertTrue(any("vocal_eq.py" in str(path) for path in vocal_eq_fingerprint_paths(root, {"skip_vocal_eq": True})))
        plain = enrich("Clean", {"preset": "light", "vocal": True})
        marked = enrich("Clean", {"preset": "light", "vocal": False, "skip_vocal_eq": True})
        self.assertFalse(any("vocal_eq.py" in key for key in plain["dependency_hashes"]))
        self.assertTrue(any("vocal_eq.py" in key for key in marked["dependency_hashes"]))
        self.assertTrue(any("cleaner.py" in key for key in plain["dependency_hashes"]))
        cleaner = (ROOT / "core" / "cleaner.py").read_text(encoding="utf-8")
        self.assertIn("highpass=f=75", cleaner)
        self.assertIn("deesser=i=0.15:m=0.3:f=0.5", cleaner)
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("vocal_extras = apply_vocal_extras(mode, params)", pipeline)
        self.assertIn('clean_parameters["skip_vocal_eq"] = True', pipeline)
        self.assertIn("vocal=vocal_extras", pipeline)
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", fingerprint)
        job_spec = (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8")
        self.assertIn("spec['schema_version'] = 1", job_spec)
