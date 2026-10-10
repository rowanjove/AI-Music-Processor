import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.component_platform import build_registry
from core.config import CONFIG_DIR
from core.storage import DEFAULT_DATABASE_PATH
from core.ux_flow import run_quick_self_test


def _stamp(path):
    if not os.path.isfile(path):
        return None
    stat = os.stat(path)
    return (stat.st_mtime_ns, stat.st_size)


class QuickSelfTestTests(unittest.TestCase):
    def test_unselected_and_missing_dual_do_not_infer(self):
        registry = build_registry(ROOT)
        components = dict(registry["components"])
        for component_id in ("engine.separator.kim_vocal_2", "engine.separator.inst_hq_3"):
            components[component_id] = dict(components.get(component_id, {}), implemented=False, status="NOT_IMPLEMENTED")
        registry = dict(registry, components=components)
        calls = []
        unselected = run_quick_self_test(registry, runner=lambda profile: calls.append(profile))
        self.assertIn("请先选择", unselected["reason"])
        self.assertFalse(unselected["inference_ran"])
        self.assertFalse(unselected["start_gpu"])
        self.assertFalse(unselected["gpu_pass"])
        blocked = run_quick_self_test(registry, chain="dual", runner=lambda profile: calls.append(profile))
        self.assertEqual(calls, [])
        self.assertFalse(blocked["inference_ran"])
        self.assertFalse(blocked["gpu_pass"])
        self.assertFalse(blocked["writes_history"])
        self.assertEqual(blocked["quality_acceptance"], "NOT_ASSESSED")
        self.assertIn("engine.separator.kim_vocal_2", blocked["missing"])
        self.assertIn("engine.separator.inst_hq_3", blocked["missing"])

    def test_ready_dual_route_calls_the_runner_without_accepting_quality(self):
        registry = build_registry(ROOT)
        components = dict(registry["components"])
        for component_id in ("engine.separator.kim_vocal_2", "engine.separator.inst_hq_3"):
            components[component_id] = dict(components[component_id], implemented=True, status="PRESENT")
        ready = dict(registry)
        ready["components"] = components
        calls = []

        def runner(profile):
            calls.append(profile)
            return {"ok": True, "inference_ran": True, "inference_device": "cpu", "stages": ["Separate"]}

        def fake_metadata(path):
            return {"primary_stem": "Vocals" if Path(path).name == "Kim_Vocal_2.onnx" else "Instrumental"}

        with tempfile.TemporaryDirectory() as folder:
            for name in ("Kim_Vocal_2.onnx", "UVR-MDX-NET-Inst_HQ_3.onnx"):
                (Path(folder) / name).write_bytes(b"onnx")
            with patch("core.separator_routing.mdx_loader_implemented", return_value=True), \
                 patch("core.mdx_separator.load_model_metadata", side_effect=fake_metadata):
                report = run_quick_self_test(ready, chain="dual", runner=runner, weight_dir=folder)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["vocal"]["parameters"], {})
        self.assertEqual(calls[0]["instrumental"]["parameters"], {})
        self.assertNotIn("agg", calls[0]["vocal"]["parameters"])
        self.assertTrue(report["inference_ran"])
        self.assertFalse(report["start_gpu"])
        self.assertFalse(report["gpu_pass"])
        self.assertEqual(report["status"], "MEASURED")
        self.assertIn("没有在 CUDA", report["reason"])
        self.assertEqual(report["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(report["listening_quality"], "not_assessed")
        self.assertFalse(report["writes_history"])

    def test_polish_self_test_runs_clean_master_and_qc_without_history(self):
        registry = build_registry(ROOT)
        before = (_stamp(DEFAULT_DATABASE_PATH), _stamp(os.path.join(CONFIG_DIR, "app.json")))
        with tempfile.TemporaryDirectory() as folder:
            report = run_quick_self_test(registry, chain="polish", work_dir=folder)
            master = report["outputs"].get("master")
            self.assertTrue(report["inference_ran"])
            self.assertFalse(report["start_gpu"])
            self.assertFalse(report["gpu_pass"])
            self.assertFalse(report["writes_history"])
            self.assertEqual(report["quality_acceptance"], "NOT_ASSESSED")
            self.assertEqual(report["listening_quality"], "not_assessed")
            self.assertEqual(report["stages_ran"], ["Repair", "Tone", "Dynamics", "Stereo", "Master", "QC"])
            self.assertTrue(master and os.path.isfile(master) and os.path.getsize(master) > 0)
            self.assertFalse(os.path.isfile(os.path.join(folder, "state.db")))
        self.assertEqual(before, (_stamp(DEFAULT_DATABASE_PATH), _stamp(os.path.join(CONFIG_DIR, "app.json"))))

    def test_voice_and_transcribe_call_the_shipped_chain(self):
        registry = build_registry(ROOT)
        calls = []

        def separate(input_wav, vocal_wav, inst_wav, model_name="HP2_all_vocals.pth", agg=10):
            calls.append(("separate", model_name, agg))
            Path(vocal_wav).write_bytes(b"vocal")
            Path(inst_wav).write_bytes(b"inst")
            return vocal_wav, inst_wav

        def convert(input_vocal_wav, output_rvc_wav, **kwargs):
            calls.append(("rvc", kwargs.get("f0_method")))
            Path(output_rvc_wav).write_bytes(b"rvc")
            return output_rvc_wav

        def mix(vocal_wav, inst_wav, output_mix_wav, add_reverb=False, **kwargs):
            calls.append(("mix", add_reverb))
            Path(output_mix_wav).write_bytes(b"mix")
            return output_mix_wav

        def four_stems(input_wav, output_dir):
            calls.append("htdemucs")
            os.makedirs(output_dir, exist_ok=True)
            vocal = os.path.join(output_dir, "vocal.wav")
            Path(vocal).write_bytes(b"vocal")
            return {"vocal": vocal, "drums": None, "bass": None, "other": None}

        def transcribe(input_path, output_midi, bpm=120):
            calls.append(("basic_pitch", bpm))
            Path(output_midi).write_bytes(b"MThd")
            return output_midi

        with tempfile.TemporaryDirectory() as folder:
            with patch("core.separator.separate_audio", separate), patch("core.rvc_engine.convert_voice", convert), patch("core.mixer.mix_stems", mix):
                voice = run_quick_self_test(registry, chain="voice_conversion", work_dir=os.path.join(folder, "voice"))
            with patch("core.separator.separate_four_stems", four_stems), patch("core.pipeline_support._transcribe", transcribe):
                notes = run_quick_self_test(registry, chain="transcribe", work_dir=os.path.join(folder, "notes"))
        self.assertEqual(calls[0], ("separate", "HP2_all_vocals.pth", 10))
        self.assertEqual(calls[1][0], "rvc")
        self.assertEqual(calls[1][1], "harvest")
        self.assertEqual(calls[2], ("mix", False))
        self.assertEqual(calls[3], "htdemucs")
        self.assertEqual(calls[4], ("basic_pitch", 120))
        self.assertTrue(voice["inference_ran"])
        self.assertFalse(voice["start_gpu"])
        self.assertFalse(voice["gpu_pass"])
        self.assertEqual(voice["stages_ran"], ["Separate", "RVC", "Mix"])
        self.assertEqual(voice["quality_acceptance"], "NOT_ASSESSED")
        self.assertFalse(voice["writes_history"])
        self.assertTrue(notes["inference_ran"])
        self.assertFalse(notes["start_gpu"])
        self.assertEqual(notes["stages_ran"], ["Separate", "Transcribe"])
        self.assertEqual(notes["quality_acceptance"], "NOT_ASSESSED")
        self.assertFalse(notes["writes_history"])

    def test_voice_self_test_keeps_separate_when_rvc_fails(self):
        registry = build_registry(ROOT)

        def separate(input_wav, vocal_wav, inst_wav, model_name="HP2_all_vocals.pth", agg=10):
            Path(vocal_wav).write_bytes(b"vocal")
            Path(inst_wav).write_bytes(b"inst")
            return vocal_wav, inst_wav

        def convert(input_vocal_wav, output_rvc_wav, **kwargs):
            raise RuntimeError("rvc failed")

        with tempfile.TemporaryDirectory() as folder:
            with patch("core.separator.separate_audio", separate), patch("core.rvc_engine.convert_voice", convert):
                report = run_quick_self_test(registry, chain="voice_conversion", work_dir=folder)
        self.assertEqual(report["stages_ran"], ["Separate"])
        self.assertTrue(report["inference_ran"])
        self.assertFalse(report["start_gpu"])
        self.assertFalse(report["gpu_pass"])
        self.assertFalse(report["writes_history"])
        self.assertEqual(report["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(report["listening_quality"], "not_assessed")
        self.assertIn("RVC 没有完成", report["reason"])

    def test_builtin_dual_self_test_uses_actual_provider_evidence(self):
        from core.ux_flow import _execute_dual
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "synthetic.wav"
            source.write_bytes(b"fixture")
            output = {}

            def separate(path, out_dir, **kwargs):
                output["call"] = (path, out_dir, kwargs)
                os.makedirs(out_dir, exist_ok=True)
                names = ("kim_vocal", "kim_instrumental", "insthq3_vocal", "insthq3_instrumental")
                result = {name: str(Path(out_dir) / (name + ".wav")) for name in names}
                for value in result.values():
                    Path(value).write_bytes(b"audio")
                worker = Path(out_dir) / "worker.json"
                worker.write_text(json.dumps({"inference_device": "cpu"}), encoding="utf-8")
                result["worker_path"] = str(worker)
                result["inference_device"] = "cpu"
                result["onnx_provider"] = {"kim_vocal_2": "CPUExecutionProvider",
                                            "inst_hq_3": "CPUExecutionProvider"}
                return result

            from core.separator_routing import dual_profile
            with patch("core.separator.separate_dual_audio", separate):
                produced = _execute_dual(dual_profile(), (folder, str(source)))
        self.assertEqual(output["call"][2]["device"], "auto")
        self.assertTrue(produced["ok"])
        self.assertTrue(produced["inference_ran"])
        self.assertEqual(produced["inference_device"], "cpu")
        self.assertEqual(produced["stages"], ["Separate"])

    def test_cuda_claim_without_worker_artifact_cannot_pass(self):
        from core.ux_flow import _finish_self_test
        report=_finish_self_test({}, {
            "ok":True,"inference_ran":True,"inference_device":"cuda",
            "stages":["Separate"],"outputs":{},
        })
        self.assertFalse(report["start_gpu"])
        self.assertFalse(report["gpu_pass"])
        self.assertFalse(report["device_evidence"]["validated"])

    def test_cuda_pass_requires_completed_worker_report_and_output_hash(self):
        from core.ux_flow import _finish_self_test
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/"output.wav"
            output.write_bytes(b"completed model output")
            sidecar=Path(folder)/"worker.json"
            sidecar.write_text(json.dumps({
                "status":"complete","selected_device":"cuda",
                "output_paths":[str(output)],
                "recovery_attempts":[{"device":"cuda","success":True}],
            }),encoding="utf-8")
            report=_finish_self_test({}, {
                "ok":True,"inference_ran":True,"inference_device":"cuda",
                "stages":["RVC"],"outputs":{"rvc":str(output)},
                "device_evidence":{"kind":"worker_recovery",
                                   "worker_report_path":str(sidecar),
                                   "expected_outputs":[str(output)]},
            })
        self.assertTrue(report["start_gpu"])
        self.assertTrue(report["gpu_pass"])
        self.assertTrue(report["device_evidence"]["validated"])
        self.assertEqual(len(report["device_evidence"]["artifact_hashes"]),1)


if __name__ == "__main__":
    unittest.main()
