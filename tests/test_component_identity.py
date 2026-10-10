import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.component_identity import capture_component_identity, declaration_sha256
from core.component_platform import catalog_path, load_catalog
from core.job_spec import CorruptJobError, digest, freeze, validate
from core.separator_routing import dual_profile

VOICE_IDS = (
    "app.core",
    "engine.rvc",
    "engine.separator.inst_hq_3",
    "engine.separator.kim_vocal_2",
    "engine.separator.mdx",
    "mode.voice_conversion",
    "model.hubert",
    "tool.ffmpeg",
    "tool.ffprobe",
)
POLISH_IDS = (
    "app.core",
    "mode.polish",
    "runtime.python",
    "tool.ffmpeg",
    "tool.ffprobe",
)
TRANSCRIBE_IDS = (
    "app.core",
    "engine.basic_pitch",
    "engine.separator.htdemucs",
    "mode.transcribe",
    "tool.ffmpeg",
    "tool.ffprobe",
)


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
    )


def _by_id():
    catalog = load_catalog(catalog_path(ROOT))
    return {item["id"]: item for item in catalog["components"]}


class ComponentIdentityTests(unittest.TestCase):
    def test_new_jobs_record_catalog_identity(self):
        first = capture_component_identity("voice_conversion", ROOT)
        second = capture_component_identity("voice_conversion", ROOT)
        self.assertEqual(first, second)
        catalog = _by_id()
        with tempfile.TemporaryDirectory() as folder:
            pth = Path(folder) / "model.pth"
            pth.write_bytes(b"weights")
            model = dict(
                name="demo", pth=str(pth), index=None,
                profile=dict(protect=0.33, rms_mix_rate=0.25, filter_radius=3),
            )
            claimed = _parameters()
            claimed["component_identity"] = [{
                "component_id": "engine.f0.rmvpe",
                "version": "vendor",
                "declaration_sha256": "a" * 64,
            }]
            with patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
                voice = freeze(claimed, "voice_conversion")
                polish = freeze(_parameters(), "polish")
                transcribe = freeze(_parameters(), "transcribe")
        self.assertEqual(voice["schema_version"], 1)
        self.assertEqual([item["component_id"] for item in voice["component_identity"]], list(VOICE_IDS))
        self.assertEqual([item["component_id"] for item in polish["component_identity"]], list(POLISH_IDS))
        self.assertEqual([item["component_id"] for item in transcribe["component_identity"]], list(TRANSCRIBE_IDS))
        versions = {item["component_id"]: item["version"] for item in voice["component_identity"]}
        self.assertEqual(versions["engine.rvc"], "RVC-beta-v2-0618")
        self.assertEqual(versions["engine.separator.mdx"], "dual-profile-adapter")
        self.assertIsNone(versions["mode.voice_conversion"])
        transcribe_versions = {item["component_id"]: item["version"] for item in transcribe["component_identity"]}
        self.assertEqual(transcribe_versions["engine.basic_pitch"], "icassp_2022")
        self.assertEqual(transcribe_versions["engine.separator.htdemucs"], "htdemucs")
        for spec in (voice, polish, transcribe):
            self.assertNotIn("engine.f0.rmvpe", [item["component_id"] for item in spec["component_identity"]])
            for item in spec["component_identity"]:
                self.assertEqual(item["declaration_sha256"], declaration_sha256(catalog[item["component_id"]]))
                self.assertEqual(item["version"], catalog[item["component_id"]].get("version"))
        module = (ROOT / "core" / "component_identity.py").read_text(encoding="utf-8")
        self.assertNotIn("build_registry", module)
        self.assertNotIn("sha256_file", module)
        self.assertNotIn("pipeline", module)
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", fingerprint)
        self.assertNotIn("stage_contract_version=3", fingerprint)
        self.assertIn("freeze(params, mode", (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8"))
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))

    def test_dual_profile_identity_includes_the_actual_mdx_adapter(self):
        identity = capture_component_identity("voice_conversion", ROOT, separator_profile=dual_profile())
        ids = {item["component_id"] for item in identity}
        self.assertIn("engine.separator.mdx", ids)
        self.assertIn("engine.separator.kim_vocal_2", ids)
        self.assertIn("engine.separator.inst_hq_3", ids)

    def test_legacy_snapshot_restores_and_bad_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            pth = Path(folder) / "model.pth"
            pth.write_bytes(b"weights")
            model = dict(
                name="demo", pth=str(pth), index=None,
                profile=dict(protect=0.33, rms_mix_rate=0.25, filter_radius=3),
            )
            with patch("core.job_spec.get_available_rvc_models", return_value={"demo": model}):
                spec = freeze(_parameters(), "voice_conversion")
                legacy = copy.deepcopy(spec)
                del legacy["component_identity"]
                legacy["spec_hash"] = digest(legacy)
                restored = validate(legacy, "voice_conversion")
                broken = copy.deepcopy(spec)
                broken["component_identity"][0]["declaration_sha256"] = "zz"
                broken["spec_hash"] = digest(broken)
                with self.assertRaises(CorruptJobError):
                    validate(broken, "voice_conversion")
                pth.write_bytes(b"changed")
                with self.assertRaises(CorruptJobError):
                    validate(spec, "voice_conversion")
        self.assertNotIn("component_identity", restored)
        self.assertEqual(restored["schema_version"], 1)
        self.assertIn("effective_rvc", restored)
