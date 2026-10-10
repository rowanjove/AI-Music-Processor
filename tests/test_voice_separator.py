import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.component_platform import catalog_path, load_catalog
from core.separator import resolve_voice_backend, separate_audio
from core.stage_operations import StageOperations

PIPELINE_CALL = "separate_runner = lambda: self._separate_and_copy(processing_wav, job_temp, stems_dir, mode,"


def _write_outputs(vocal_wav, inst_wav):
    os.makedirs(os.path.dirname(vocal_wav), exist_ok=True)
    for path in (vocal_wav, inst_wav, vocal_wav + ".worker.json"):
        with open(path, "wb") as handle:
            handle.write(b"x")


class VoiceSeparatorTests(unittest.TestCase):
    def test_default_backend_is_hp2_and_unknown_ids_fail(self):
        default = resolve_voice_backend()
        named = resolve_voice_backend("hp2")
        self.assertEqual(default, named)
        self.assertEqual(default["backend"], "hp2")
        self.assertEqual(default["model_name"], "HP2_all_vocals.pth")
        self.assertEqual(default["agg"], 10)
        signature = inspect.signature(separate_audio)
        self.assertEqual(signature.parameters["model_name"].default, default["model_name"])
        self.assertEqual(signature.parameters["agg"].default, default["agg"])
        for backend in ("mdx", "roformer", "", "HP2", "HP2_all_vocals.pth", 10):
            with self.assertRaises(ValueError):
                resolve_voice_backend(backend)

    def test_voice_stage_keeps_hp2_paths_and_rejects_before_the_worker(self):
        spec = resolve_voice_backend()
        with tempfile.TemporaryDirectory(prefix="voice-separator-") as folder:
            job = os.path.join(folder, "job")
            stems = os.path.join(folder, "stems")
            os.makedirs(stems)
            seen = {}

            def fake_separate(input_wav, vocal_wav, inst_wav, model_name=None, agg=None):
                seen["model_name"] = model_name
                seen["agg"] = agg
                _write_outputs(vocal_wav, inst_wav)
                return vocal_wav, inst_wav

            with mock.patch("core.stage_operations.separate_audio", side_effect=fake_separate):
                result = StageOperations._separate_and_copy(
                    os.path.join(folder, "in.wav"), job, stems, "voice_conversion",
                )
            self.assertEqual(seen["model_name"], spec["model_name"])
            self.assertEqual(seen["agg"], spec["agg"])
            self.assertTrue(result["vocal"].endswith(os.path.join("stems", "01_vocal_original.wav")))
            self.assertTrue(result["instrumental"].endswith(os.path.join("stems", "04_instrumental.wav")))
            self.assertEqual(result["other"], result["instrumental"])
            self.assertIsNone(result["drums"])
            self.assertIsNone(result["bass"])
            with mock.patch("core.stage_operations.separate_audio") as worker:
                with self.assertRaises(ValueError):
                    StageOperations._separate_and_copy(
                        os.path.join(folder, "in.wav"), job, stems, "voice_conversion", backend="mdx",
                    )
                worker.assert_not_called()

    def test_pipeline_parameters_and_catalog_stay_on_hp2(self):
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        stage = (ROOT / "core" / "stage_operations.py").read_text(encoding="utf-8")
        self.assertIn(PIPELINE_CALL, pipeline)
        self.assertIn('"model": "four-stem-addon-or-HP2"', pipeline)
        self.assertNotIn("HP2_all_vocals.pth", stage)
        self.assertIn("01_vocal_original.wav", stage)
        self.assertIn("04_instrumental.wav", stage)
        catalog = load_catalog(catalog_path(ROOT))
        by_id = {item["id"]: item for item in catalog["components"]}
        hp2 = by_id["engine.separator.hp2"]
        self.assertTrue(hp2["implemented"])
        self.assertEqual(hp2["version"], "HP2_all_vocals")
        self.assertIn("hp2", hp2["version_evidence"])
        self.assertIn("resolve_voice_backend", hp2["version_evidence"])
        self.assertTrue(by_id["engine.separator.mdx"]["implemented"])
        self.assertTrue(by_id["engine.separator.roformer"]["implemented"])
        self.assertEqual(by_id["engine.separator.roformer"]["details"]["minimum_python_version"], [3, 10])
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=2", fingerprint)
        self.assertNotIn("stage_contract_version=3", fingerprint)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
