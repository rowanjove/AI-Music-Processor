import sys
import unittest
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.component_platform import build_registry, capability_report, has_capability
from core.diagnostic_registry import registry_status
from core.diagnostics import diagnostics


class DiagnosticRegistryTests(unittest.TestCase):
    def test_registry_view_separates_runnable_rmvpe_from_unassessed_quality(self):
        registry = build_registry(ROOT)
        report = capability_report(registry)
        view = registry_status(registry)
        self.assertTrue(has_capability(report, "pitch.rmvpe"))
        self.assertTrue(view["pitch_rmvpe_available"])
        self.assertTrue(view['rmvpe_runnable'])
        self.assertEqual(view['rmvpe_acceptance_status'], 'READY_FOR_EXTERNAL_VALIDATION')
        self.assertEqual(view["engine_f0_rmvpe_status"], "PRESENT")
        self.assertEqual(registry["components"]["engine.f0.rmvpe"]["status"], "PRESENT")
        self.assertNotEqual(view["engine_f0_rmvpe_status"], "PASS")
        ids = [item["id"] for item in view["components"]]
        self.assertIn("engine.f0.rmvpe", ids)
        self.assertEqual(view["source"], "component_registry")
        platform = (ROOT / "core" / "component_platform.py").read_text(encoding="utf-8")
        self.assertNotIn("import core.pipeline", platform)
        self.assertNotIn("import core.rvc_engine", platform)
        self.assertNotIn("import core.separator", platform)
        build = platform.split("def build_registry", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("verify_runtime", build)
        diagnostics_source = (ROOT / "core" / "diagnostics.py").read_text(encoding="utf-8")
        self.assertIn("status='PASS'", diagnostics_source)
        self.assertIn("status='FAIL'", diagnostics_source)
        self.assertIn("registry_status(build_registry(BASE_DIR))", diagnostics_source)
        self.assertEqual(diagnostics_source.count("verify_runtime("), 1)
        for key in ("FFmpeg", "ffprobe", "UVR two-stem", "Runtime manifest", "Disk"):
            self.assertIn("'" + key + "'", diagnostics_source.replace('"', "'"))
        self.assertIn("pitch_rmvpe_available", diagnostics_source)
        result = diagnostics()
        for key in ("FFmpeg", "ffprobe", "Python/AI", "UVR two-stem", "Demucs four-stem",
                    "HuBERT", "Basic Pitch", "Audacity", "MuseScore", "LMMS", "NeuralNote",
                    "RVC models", "Runtime manifest", "Disk", "Components"):
            self.assertIn(key, result)
        self.assertIn(result["FFmpeg"]["status"], ("PASS", "FAIL"))
        self.assertIn(result["UVR two-stem"]["status"], ("PASS", "FAIL"))
        self.assertTrue(result["Components"]["pitch_rmvpe_available"])
        self.assertNotEqual(result["Components"].get("engine_f0_rmvpe_status"), "PASS")
        self.assertEqual(result["Components"].get("engine_f0_rmvpe_status"), "PRESENT")
        with tempfile.TemporaryDirectory() as folder:
            missing_registry = build_registry(folder, catalog=registry['catalog'])
            missing_view = registry_status(missing_registry)
            self.assertFalse(has_capability(capability_report(missing_registry), 'pitch.rmvpe'))
            self.assertFalse(missing_view['pitch_rmvpe_available'])
            self.assertFalse(missing_view['rmvpe_runnable'])
            self.assertEqual(missing_view['engine_f0_rmvpe_status'], 'MISSING')


if __name__ == "__main__":
    unittest.main()
