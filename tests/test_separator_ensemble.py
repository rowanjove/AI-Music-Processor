import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from core import separator_ensemble as ensemble
from core.separator_benchmark import run_ensemble_experiment


def _write(path, value, frames=480, rate=48000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = np.full((frames, 2), value, dtype=np.float32)
    sf.write(str(path), audio, rate, format="WAV", subtype="FLOAT")
    return path


def _candidate_hp2(output_dir, vocal_value, instrumental_value):
    root = Path(output_dir)
    root.mkdir(parents=True)
    vocal = _write(root / "vocal.wav", vocal_value)
    instrumental = _write(root / "instrumental.wav", instrumental_value)
    evidence = root / "candidate-worker-evidence.json"
    report = {
        "status": "EXECUTED",
        "worker": "uvr_candidate_exact_length",
        "length_adapter": {
            "adapter": "candidate_local_exact_istft_length",
            "band_source_lengths": [],
            "resample_length_corrections": [],
        },
        "quality_acceptance": "NOT_ASSESSED",
    }
    evidence.write_text(json.dumps(report), encoding="utf-8")
    sidecar = root / "worker-sidecar.json"
    sidecar.write_text(json.dumps({"status": "complete", "selected_device": "cpu"}), encoding="utf-8")
    return {"vocal": str(vocal), "instrumental": str(instrumental), "evidence": str(evidence),
            "report": report, "worker_sidecar": str(sidecar)}


class SeparatorEnsembleTests(unittest.TestCase):
    def test_standardization_pads_only_a_single_short_final_sample(self):
        with tempfile.TemporaryDirectory(prefix="ensemble-pad-") as folder:
            source = _write(Path(folder) / "short.wav", 0.25, frames=480)
            output = Path(folder) / "normalized.wav"

            def copy_float(src, dst):
                audio, rate = sf.read(str(src), dtype="float32", always_2d=True)
                sf.write(str(dst), audio, rate, format="WAV", subtype="FLOAT")

            with patch("core.analyzer.standardize_to_48k_float", side_effect=copy_float):
                result = ensemble.standardize_stem_to_reference(source, output, 481)
            self.assertEqual(result["frame_adjustment"], {"kind": "pad_end_repeat_last_frame", "samples": 1})
            self.assertEqual(sf.info(str(output)).frames, 481)
            self.assertEqual(sf.info(str(output)).subtype, "FLOAT")

    def test_standardization_rejects_extra_frame_without_truncating(self):
        with tempfile.TemporaryDirectory(prefix="ensemble-extra-") as folder:
            source = _write(Path(folder) / "long.wav", 0.25, frames=481)
            output = Path(folder) / "normalized.wav"

            def copy_float(src, dst):
                audio, rate = sf.read(str(src), dtype="float32", always_2d=True)
                sf.write(str(dst), audio, rate, format="WAV", subtype="FLOAT")

            with patch("core.analyzer.standardize_to_48k_float", side_effect=copy_float):
                with self.assertRaisesRegex(ValueError, "不会截短"):
                    ensemble.standardize_stem_to_reference(source, output, 480)
            self.assertFalse(output.exists())

    def test_raw_backend_stems_must_already_be_stereo_finite_wavs(self):
        with tempfile.TemporaryDirectory(prefix="ensemble-raw-") as folder:
            root = Path(folder)
            mono = root / "mono.wav"
            sf.write(str(mono), np.zeros(480, dtype=np.float32), 44100, format="WAV", subtype="PCM_16")
            with patch("core.analyzer.standardize_to_48k_float") as standardize:
                with self.assertRaisesRegex(ValueError, "双声道 WAV"):
                    ensemble.standardize_stem_to_reference(mono, root / "out.wav", 480)
                standardize.assert_not_called()

            invalid = root / "nan.wav"
            sf.write(str(invalid), np.full((480, 2), np.nan, dtype=np.float32), 44100,
                     format="WAV", subtype="FLOAT")
            with patch("core.analyzer.standardize_to_48k_float") as standardize:
                with self.assertRaisesRegex(FloatingPointError, "NaN/Inf"):
                    ensemble.standardize_stem_to_reference(invalid, root / "out-nan.wav", 480)
                standardize.assert_not_called()

    def test_mean_requires_exact_shape_finite_audio_and_does_not_overwrite_source(self):
        with tempfile.TemporaryDirectory(prefix="ensemble-mean-") as folder:
            root = Path(folder)
            reference = _write(root / "source.wav", 0.0)
            hp2 = _write(root / "hp2.wav", 0.2)
            dual = _write(root / "dual.wav", 0.4)
            source_sha = ensemble.sha256_file(reference)
            result = ensemble.combine_aligned_mean(reference, hp2, dual, root / "mean.wav")
            mean, rate = sf.read(result["path"], dtype="float32", always_2d=True)
            self.assertEqual(rate, 48000)
            self.assertTrue(np.allclose(mean, 0.3, atol=1e-7))
            self.assertEqual(ensemble.sha256_file(reference), source_sha)
            self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")
            with self.assertRaises(FileExistsError):
                ensemble.combine_aligned_mean(reference, hp2, dual, root / "mean.wav")
            with self.assertRaises(ValueError):
                ensemble.combine_aligned_mean(reference, hp2, dual, reference)

            extra = _write(root / "extra.wav", 0.5, frames=481)
            with self.assertRaisesRegex(ValueError, "帧数不符"):
                ensemble.combine_aligned_mean(reference, hp2, extra, root / "bad-mean.wav")

    def test_experiment_freezes_source_engines_and_artifacts_without_selecting_backend(self):
        with tempfile.TemporaryDirectory(prefix="ensemble-run-") as folder:
            root = Path(folder)
            source = _write(root / "input.wav", 0.1)
            hp2_weights = root / "weights"
            hp2_weights.mkdir()
            (hp2_weights / "HP2_all_vocals.pth").write_bytes(b"hp2 frozen weights")
            mdx_weights = root / "mdx"
            mdx_weights.mkdir()
            identities = {
                "vocal": {"path": str(mdx_weights / "Kim_Vocal_2.onnx"), "sha256": "a" * 64},
                "instrumental": {"path": str(mdx_weights / "UVR-MDX-NET-Inst_HQ_3.onnx"), "sha256": "b" * 64},
            }

            def standardize(src, dst):
                audio, rate = sf.read(str(src), dtype="float32", always_2d=True)
                sf.write(str(dst), audio, rate, format="WAV", subtype="FLOAT")
                return dst

            def run_hp2(src, output_dir, **kwargs):
                return _candidate_hp2(output_dir, 0.2, 0.3)

            def run_dual(src, output_dir, **kwargs):
                output_dir = Path(output_dir)
                paths = {
                    "kim_vocal": _write(output_dir / "kim_vocal.wav", 0.4),
                    "kim_instrumental": _write(output_dir / "kim_instrumental.wav", 0.6),
                    "insthq3_vocal": _write(output_dir / "insthq3_vocal.wav", 0.45),
                    "insthq3_instrumental": _write(output_dir / "insthq3_instrumental.wav", 0.5),
                }
                worker = output_dir / "worker.json"
                worker.write_text(json.dumps({"status": "complete", "provider": "CPUExecutionProvider"}), encoding="utf-8")
                paths["worker_path"] = worker
                paths.update({"selected_device": "cpu", "device": "cpu", "inference_device": "cpu",
                              "onnx_provider": "CPUExecutionProvider", "models_execution": []})
                return paths

            source_sha = ensemble.sha256_file(source)
            with patch("core.analyzer.standardize_to_48k_float", side_effect=standardize), \
                    patch("core.uvr_candidate_separator.separate_candidate_hp2", side_effect=run_hp2), \
                    patch("core.separator.separate_dual_audio", side_effect=run_dual), \
                    patch("core.mdx_separator.dual_model_identities", return_value=identities), \
                    patch("core.separator_benchmark.plan_separator_routes", return_value={"available": True}), \
                    patch("core.separator_benchmark._runtime_identity", return_value={"executable": {"sha256": "c" * 64}}), \
                    patch("core.separator_benchmark._code_identity", return_value={"engine.py": "d" * 64}):
                result = run_ensemble_experiment(
                    source, root / "experiment", hp2_weight_dir=hp2_weights, mdx_weight_dir=mdx_weights,
                )

            self.assertEqual(result["status"], "EXECUTED_NOT_ASSESSED")
            self.assertEqual(result["source"]["sha256_before"], source_sha)
            self.assertEqual(result["source"]["sha256_after"], source_sha)
            self.assertFalse(result["job_selectable"])
            self.assertFalse(result["selectable_changed"])
            self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")
            self.assertIn("elapsed_seconds", result["runs"]["hp2"])
            self.assertEqual(result["runs"]["hp2"]["engine"],
                             "candidate-local bundled UVR5 VR worker with measured band ISTFT lengths")
            self.assertEqual(result["runs"]["hp2"]["worker_evidence"]["length_adapter"]["adapter"],
                             "candidate_local_exact_istft_length")
            self.assertIn("elapsed_seconds", result["runs"]["dual"])
            self.assertEqual(result["runs"]["dual"]["execution_summary"]["onnx_provider"], "CPUExecutionProvider")
            self.assertEqual(result["runs"]["ensemble"]["combiner"], "samplewise_arithmetic_mean")
            mean, _ = sf.read(result["runs"]["ensemble"]["outputs"]["vocal"]["path"], dtype="float32", always_2d=True)
            self.assertTrue(np.allclose(mean, 0.3, atol=1e-7))
            self.assertTrue(Path(result["runs"]["hp2"]["raw_outputs"]["vocal"]["path"]).is_file())
            self.assertTrue(Path(result["runs"]["dual"]["raw_outputs"]["kim_vocal"]["path"]).is_file())
            manifest = json.loads((root / "experiment" / "experiment.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "EXECUTED_NOT_ASSESSED")

    def test_failed_length_gate_keeps_raw_engine_evidence_in_manifest(self):
        with tempfile.TemporaryDirectory(prefix="ensemble-failed-run-") as folder:
            root = Path(folder)
            source = _write(root / "input.wav", 0.1)
            hp2_weights = root / "weights"
            hp2_weights.mkdir()
            (hp2_weights / "HP2_all_vocals.pth").write_bytes(b"hp2 frozen weights")
            mdx_weights = root / "mdx"
            mdx_weights.mkdir()
            identities = {
                "vocal": {"path": str(mdx_weights / "Kim_Vocal_2.onnx"), "sha256": "a" * 64},
                "instrumental": {"path": str(mdx_weights / "UVR-MDX-NET-Inst_HQ_3.onnx"), "sha256": "b" * 64},
            }

            def standardize(src, dst):
                audio, rate = sf.read(str(src), dtype="float32", always_2d=True)
                sf.write(str(dst), audio, rate, format="WAV", subtype="FLOAT")
                return dst

            def run_hp2(src, output_dir, **kwargs):
                return _candidate_hp2(output_dir, 0.2, 0.3)

            experiment = root / "experiment"
            with patch("core.analyzer.standardize_to_48k_float", side_effect=standardize), \
                    patch("core.uvr_candidate_separator.separate_candidate_hp2", side_effect=run_hp2), \
                    patch("core.mdx_separator.dual_model_identities", return_value=identities), \
                    patch("core.separator_benchmark.plan_separator_routes", return_value={"available": True}), \
                    patch("core.separator_benchmark._runtime_identity", return_value={"executable": {"sha256": "c" * 64}}), \
                    patch("core.separator_benchmark._code_identity", return_value={"engine.py": "d" * 64}), \
                    patch("core.separator_ensemble.standardize_stem_to_reference", side_effect=ValueError("length mismatch")):
                with self.assertRaisesRegex(ValueError, "length mismatch"):
                    run_ensemble_experiment(
                        source, experiment, hp2_weight_dir=hp2_weights, mdx_weight_dir=mdx_weights,
                    )

            manifest = json.loads((experiment / "experiment.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "FAILED")
            self.assertEqual(manifest["error"]["stage"], "normalize_hp2_vocal")
            self.assertEqual(manifest["runs"]["hp2"]["status"], "EXECUTED_RAW")
            self.assertTrue(Path(manifest["runs"]["hp2"]["raw_outputs"]["vocal"]["path"]).is_file())
            self.assertNotIn("dual", manifest["runs"])

    def test_changed_hp2_weight_is_detected_after_run_and_prevents_dual(self):
        with tempfile.TemporaryDirectory(prefix="ensemble-changed-weight-") as folder:
            root = Path(folder)
            source = _write(root / "input.wav", 0.1)
            hp2_weights = root / "weights"
            hp2_weights.mkdir()
            weight = hp2_weights / "HP2_all_vocals.pth"
            weight.write_bytes(b"hp2 frozen weights")
            mdx_weights = root / "mdx"
            mdx_weights.mkdir()
            identities = {
                "vocal": {"path": str(mdx_weights / "Kim_Vocal_2.onnx"), "sha256": "a" * 64},
                "instrumental": {"path": str(mdx_weights / "UVR-MDX-NET-Inst_HQ_3.onnx"), "sha256": "b" * 64},
            }

            def standardize(src, dst):
                audio, rate = sf.read(str(src), dtype="float32", always_2d=True)
                sf.write(str(dst), audio, rate, format="WAV", subtype="FLOAT")
                return dst

            def run_hp2(src, output_dir, **kwargs):
                result = _candidate_hp2(output_dir, 0.2, 0.3)
                weight.write_bytes(b"changed HP2 weights")
                return result

            experiment = root / "experiment"
            dual = Mock()
            with patch("core.analyzer.standardize_to_48k_float", side_effect=standardize), \
                    patch("core.uvr_candidate_separator.separate_candidate_hp2", side_effect=run_hp2), \
                    patch("core.separator.separate_dual_audio", dual), \
                    patch("core.mdx_separator.dual_model_identities", return_value=identities), \
                    patch("core.separator_benchmark.plan_separator_routes", return_value={"available": True}), \
                    patch("core.separator_benchmark._runtime_identity", return_value={"executable": {"sha256": "c" * 64}}), \
                    patch("core.separator_benchmark._code_identity", return_value={"engine.py": "d" * 64}):
                with self.assertRaisesRegex(RuntimeError, "HP2 权重在实验期间发生变化"):
                    run_ensemble_experiment(
                        source, experiment, hp2_weight_dir=hp2_weights, mdx_weight_dir=mdx_weights,
                    )

            dual.assert_not_called()
            manifest = json.loads((experiment / "experiment.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["error"]["stage"], "verify_hp2_identity_after_run")
            self.assertNotEqual(manifest["runs"]["hp2"]["model"]["sha256"],
                                manifest["runs"]["hp2"]["model_after"]["sha256"])
            self.assertNotIn("ensemble", manifest["runs"])


if __name__ == "__main__":
    unittest.main()
