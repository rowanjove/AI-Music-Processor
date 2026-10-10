import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import f0_method_fingerprint_paths
from core.component_platform import build_registry, capability_report, catalog_path, has_capability, load_catalog
from core.config import _validate_model_profile
from core.f0_method import ABSENT_REASON, PRESENT_REASON, apply_f0_to_stage, resolve_f0_method
from core.pipeline import AudioPipeline
from core.rvc_engine import convert_voice


def _stage(f0):
    return {"model": "zhoujielun", "pitch_shift": 0, "f0_method": f0, "index_rate": 0.65, "effective_rvc": {}}


class F0MethodTests(unittest.TestCase):
    def test_direct_methods_keep_the_request(self):
        for name in ("harvest", "pm", "crepe"):
            decision = resolve_f0_method(name)
            self.assertEqual(decision["requested"], name)
            self.assertEqual(decision["executed"], name)
            self.assertFalse(decision["fallback"])
            self.assertEqual(decision["reason"], "")
            stage = _stage(name)
            updated, executed = apply_f0_to_stage(stage, name)
            self.assertEqual(executed, name)
            self.assertEqual(updated, stage)
            self.assertNotIn("f0_requested", updated)

    def test_missing_weight_falls_back_to_harvest(self):
        with tempfile.TemporaryDirectory() as folder:
            decision = resolve_f0_method("rmvpe", root=folder)
            updated, executed = apply_f0_to_stage(_stage("rmvpe"), "rmvpe", root=folder)
        self.assertEqual(decision["executed"], "harvest")
        self.assertTrue(decision["fallback"])
        self.assertEqual(decision["reason"], ABSENT_REASON)
        self.assertIn("不存在", decision["reason"])
        self.assertIn("0618", decision["reason"])
        self.assertEqual(executed, "harvest")
        self.assertEqual(updated["f0_method"], "harvest")
        self.assertEqual(updated["f0_requested"], "rmvpe")
        self.assertEqual(updated["f0_fallback_reason"], ABSENT_REASON)

    def test_empty_weight_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "rmvpe.pt"), "wb").close()
            with self.assertRaises(ValueError):
                resolve_f0_method("rmvpe", root=folder)

    def test_present_weight_routes_to_adapter_and_is_not_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "rmvpe.pt")
            with open(path, "wb") as handle:
                handle.write(b"not-a-network")
            with open(path, "rb") as handle:
                before = handle.read()
            decision = resolve_f0_method("rmvpe", root=folder)
            with open(path, "rb") as handle:
                after = handle.read()
        self.assertEqual(before, after)
        self.assertEqual(decision["executed"], "rmvpe")
        self.assertFalse(decision["fallback"])
        self.assertEqual(decision["reason"], "")

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_f0_method("dio")

    def test_worker_routes_rmvpe_without_modifying_legacy_vendor(self):
        worker = (ROOT / "core" / "rvc_worker.py").read_text(encoding="utf-8")
        cli = (ROOT / "process_music.py").read_text(encoding="utf-8")
        gui = (ROOT / "AI_Music_Processor.pyw").read_text(encoding="utf-8")
        vendor = (ROOT / "tools" / "RVC" / "RVC-beta-v2-0618" / "vc_infer_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("choices=['harvest', 'pm', 'crepe', 'rmvpe']", worker)
        self.assertIn("rmvpe_vc_class", worker)
        self.assertNotIn("rmvpe", vendor)
        self.assertIn("schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("stage_contract_version=", fingerprint)

    def test_pipeline_accepts_the_request_and_harvest_parameters_stay_plain(self):
        AudioPipeline._validate_parameters(
            "polish", "light", "natural", "", 0, "rmvpe", 0.65, {"wav"}, 48000, 120, check_model=False,
        )
        with self.assertRaises(ValueError):
            AudioPipeline._validate_parameters(
                "polish", "light", "natural", "", 0, "dio", 0.65, {"wav"}, 48000, 120, check_model=False,
            )
        root = Path("workspace")
        self.assertEqual(f0_method_fingerprint_paths(root, _stage("harvest")), [])
        paths = f0_method_fingerprint_paths(root, {"f0_method": "harvest", "f0_requested": "rmvpe"})
        self.assertIn(root / "core" / "f0_method.py", paths)
        self.assertIn(root / "core" / "rmvpe_adapter.py", paths)
        self.assertTrue(any(path.name == 'rmvpe.pt' for path in paths))

    def test_direct_call_sends_harvest_to_the_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "in.wav")
            dest = os.path.join(folder, "out.wav")
            with open(source, "wb") as handle:
                handle.write(b"x")
            seen = {}

            def fake_run(cmd, outputs, **kwargs):
                seen["cmd"] = cmd
                with open(outputs[0], "wb") as handle:
                    handle.write(b"wav")

            profile = {"pth": source, "index": "", "protect": 0.33, "rms_mix_rate": 0.25, "filter_radius": 3}
            with mock.patch("core.rvc_engine.run_worker_with_recovery", side_effect=fake_run), \
                 mock.patch("core.f0_method.resolve_f0_method", return_value={"executed": "harvest"}):
                convert_voice(source, dest, f0_method="rmvpe", profile=profile)
            argv = seen["cmd"]
            self.assertEqual(argv[argv.index("--f0") + 1], "harvest")
            self.assertEqual(argv[argv.index('--output-precision') + 1], 'legacy_int16')
            self.assertNotIn("rmvpe", argv)

    def test_catalog_rmvpe_probes_actual_weight(self):
        catalog = load_catalog(catalog_path(ROOT))
        item = next(component for component in catalog["components"] if component["id"] == "engine.f0.rmvpe")
        engine = next(component for component in catalog["components"] if component["id"] == "engine.rvc")
        self.assertTrue(any(probe.get('path') == 'tools/RVC/RVC-beta-v2-0618/rmvpe.pt'
                            for probe in item['probes']))
        registry = build_registry(ROOT)
        record = registry["components"]["engine.f0.rmvpe"]
        if item['implemented']:
            self.assertEqual(record['status'], 'PRESENT')
            self.assertTrue(has_capability(capability_report(registry), 'pitch.rmvpe'))
        else:
            self.assertFalse(has_capability(capability_report(registry), 'pitch.rmvpe'))


if __name__ == "__main__":
    unittest.main()
