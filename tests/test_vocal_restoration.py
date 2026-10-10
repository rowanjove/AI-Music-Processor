import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.vocal_restoration import WEIGHT_PRESENT, freeze_effect, restoration_catalog, run_effect


class VocalRestorationTests(unittest.TestCase):
    def test_present_weights_stay_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "VR-DeEchoNormal.pth"), "wb") as handle:
                handle.write(b"x")
            catalog = restoration_catalog(folder)
        by_id = {row["id"]: row for row in catalog["effects"]}
        self.assertFalse(catalog["applied_by_job"])
        self.assertEqual(catalog["default_enabled"], [])
        self.assertEqual(by_id["de_echo_normal"]["status"], WEIGHT_PRESENT)
        self.assertFalse(by_id["de_echo_normal"]["enabled_by_job"])
        self.assertEqual(by_id["de_echo_normal"]["benchmark_status"], "NOT_RUN")
        self.assertEqual(by_id["de_echo_aggressive"]["status"], "WEIGHT_MISSING")
        self.assertEqual(by_id["onnx_dereverb"]["status"], "WEIGHT_MISSING")
        self.assertFalse(by_id["onnx_dereverb"]["available_by_job"])
        self.assertEqual(by_id["denoise"]["status"], "CLASSICAL_IMPLEMENTED")
        self.assertEqual(by_id["bleed_suppression"]["status"], "CLASSICAL_IMPLEMENTED")
        self.assertEqual(catalog["quality_acceptance"], "NOT_ASSESSED")

    def test_classical_effect_rejects_invalid_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "in.wav")
            with open(source, "wb") as handle:
                handle.write(b"not-audio")
            result = run_effect(source, "denoise", folder)
        self.assertEqual(result["benchmark_status"], "FAILED")
        self.assertFalse(result["enabled_by_job"])
        self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")

    def test_workspace_weights_are_not_the_job_default(self):
        catalog = restoration_catalog()
        by_id = {row["id"]: row for row in catalog["effects"]}
        self.assertFalse(catalog["applied_by_job"])
        if by_id["de_echo_normal"]["weight_present"]:
            self.assertEqual(by_id["de_echo_normal"]["status"], WEIGHT_PRESENT)
            self.assertEqual(by_id["de_echo_normal"]["benchmark_status"], "NOT_RUN")
        self.assertEqual(by_id["denoise"]["status"], "CLASSICAL_IMPLEMENTED")
        self.assertEqual(by_id["bleed_suppression"]["status"], "CLASSICAL_IMPLEMENTED")

    def test_a_measured_effect_reports_the_other_stem_and_stays_off(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "VR-DeEchoNormal.pth"), "wb") as handle:
                handle.write(b"x")
            source = os.path.join(folder, "in.wav")
            tone = np.full(480, 0.4, np.float32)
            sf.write(source, np.stack([tone, tone], axis=1), 48000)

            def fake_separate(_source, vocal, residual, model_name, agg):
                self.assertEqual(model_name, "VR-DeEchoNormal.pth")
                self.assertEqual(agg, 10)
                quiet = np.full(480, 0.05, np.float32)
                loud = np.full(480, 0.4, np.float32)
                sf.write(vocal, np.stack([quiet, quiet], axis=1), 48000)
                sf.write(residual, np.stack([loud, loud], axis=1), 48000)

            with mock.patch("core.separator.separate_audio", side_effect=fake_separate):
                result = run_effect(source, "de_echo_normal", folder, weight_dir=folder)
            self.assertTrue(os.path.isfile(result["report_path"]))
        self.assertEqual(result["benchmark_status"], "MEASURED")
        self.assertTrue(result["enabled_by_job"])
        self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")
        self.assertLess(result["after"]["sample_peak"], result["residual"]["sample_peak"])
        self.assertLess(result["vocal_peak_delta"], 0)
        self.assertTrue(result["source_sha256"])
        self.assertTrue(result["weight_sha256"])

    def test_frozen_effect_identity_is_required_for_job_execution(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as folder:
            weights = os.path.join(folder, "weights")
            os.makedirs(weights)
            weight_path = os.path.join(weights, "VR-DeEchoNormal.pth")
            Path(weight_path).write_bytes(b"fixed weights")
            identity = freeze_effect("de_echo_normal", weights)
            source = os.path.join(folder, "in.wav")
            tone = np.full(480, 0.4, np.float32)
            sf.write(source, np.stack([tone, tone], axis=1), 48000)

            def fake_separate(_source, vocal, residual, model_name, agg):
                sf.write(vocal, np.stack([tone, tone], axis=1), 48000)
                sf.write(residual, np.stack([tone, tone], axis=1), 48000)

            with mock.patch("core.separator.separate_audio", side_effect=fake_separate):
                result = run_effect(source, "de_echo_normal", os.path.join(folder, "out"),
                                    weight_dir=weights, expected_identity=identity)
            Path(weight_path).write_bytes(b"changed weights")
            rejected = run_effect(source, "de_echo_normal", os.path.join(folder, "again"),
                                  weight_dir=weights, expected_identity=identity)
        self.assertEqual(result["benchmark_status"], "MEASURED")
        self.assertEqual(rejected["benchmark_status"], "FAILED")
        self.assertIn("冻结身份不一致", rejected["error"])

    def test_recorded_deecho_is_not_enabled(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "vocal_restoration_vcq025.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["benchmark_status"], "MEASURED")
        self.assertFalse(fixture["enabled_by_job"])
        self.assertEqual(fixture["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(fixture["listening_quality"], "not_assessed")
        self.assertEqual(fixture["before_peak"], 0.571624755859375)
        self.assertEqual(fixture["vocal_labeled_peak"], 0.0908203125)
        self.assertEqual(fixture["other_stem_peak"], 0.56866455078125)
        self.assertLess(fixture["vocal_labeled_peak"], fixture["other_stem_peak"])
        self.assertEqual(fixture["other_effects"], "NOT_RUN")

    def test_classical_gate_uses_measured_nonvocal_frames_and_writes_float_pair(self):
        import numpy as np
        import soundfile as sf

        rate = 24000
        rng = np.random.default_rng(7)
        noise = rng.normal(0, .004, rate).astype(np.float32)
        time = np.arange(rate, dtype=np.float32) / rate
        voice = (.16 * np.sin(2 * np.pi * 220 * time) + rng.normal(0, .004, rate)).astype(np.float32)
        source = np.concatenate([noise, voice])
        with tempfile.TemporaryDirectory() as folder:
            source_path = os.path.join(folder, "input.wav")
            sf.write(source_path, source, rate, subtype="FLOAT")
            result = run_effect(source_path, "denoise", folder)
            restored, restored_rate = sf.read(result["restored_path"], dtype="float32")
            residual, residual_rate = sf.read(result["residual_path"], dtype="float32")
            info = sf.info(result["restored_path"])
        self.assertEqual(result["benchmark_status"], "MEASURED")
        self.assertEqual(result["dsp_observation"]["algorithm"], "conservative-nonvocal-gate-v1")
        self.assertEqual((restored_rate, residual_rate, info.subtype), (rate, rate, "FLOAT"))
        self.assertEqual(len(restored), len(source))
        self.assertTrue(np.isfinite(restored).all())
        self.assertTrue(np.isfinite(residual).all())
        if result["dsp_observation"]["status"] == "APPLIED":
            self.assertLess(float(np.sqrt(np.mean(restored[:rate] ** 2))),
                            float(np.sqrt(np.mean(source[:rate] ** 2))))

    def test_frozen_onnx_dereverb_identity_routes_to_isolated_worker(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as folder:
            weights = os.path.join(folder, "weights")
            model_dir = os.path.join(weights, "onnx_dereverb_By_FoxJoy")
            os.makedirs(model_dir)
            weight = os.path.join(model_dir, "vocals.onnx")
            Path(weight).write_bytes(b"frozen FoxJoy test weights")
            identity = freeze_effect("onnx_dereverb", weights)
            source = os.path.join(folder, "input.wav")
            samples = np.full((240, 2), .2, dtype=np.float32)
            sf.write(source, samples, 24000, subtype="FLOAT")

            def fake_worker(_source, vocal, residual, _weight, weight_sha, _output_dir):
                sf.write(vocal, samples * .5, 24000, subtype="FLOAT")
                sf.write(residual, samples * .5, 24000, subtype="FLOAT")
                with open(vocal + ".worker.json", "w", encoding="utf-8") as handle:
                    json.dump({"status": "complete", "inference_ran": True,
                               "effect_id": "onnx_dereverb", "weight_sha256_before": weight_sha,
                               "weight_sha256_after": weight_sha,
                               "output_roles": ["restored vocal", "removed component"],
                               "output_subtype": "FLOAT", "session_providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
                               "inference_providers": ["CPUExecutionProvider"]}, handle)

            with mock.patch("core.vocal_restoration._run_mdxnet_effect", side_effect=fake_worker):
                record = run_effect(source, "onnx_dereverb", os.path.join(folder, "out"),
                                    weight_dir=weights, expected_identity=identity)
        self.assertEqual(record["benchmark_status"], "MEASURED")
        self.assertEqual(record["backend"], "mdxnet")
        self.assertEqual(record["weight_sha256"], identity["sha256"])
        self.assertEqual(record["output_roles"]["processed.wav"], "restored vocal")
