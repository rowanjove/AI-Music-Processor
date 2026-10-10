import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import stage_quality_trace_fingerprint_paths
from core.stage_quality_trace import (
    HEADING,
    append_stage_trace,
    audition_rows,
    build_stage_trace,
    diagnose_stages,
    load_trace,
    project_dir_from_manifest,
    write_listening_marks,
)


def _write_wav(path, kind, seconds=0.25, rate=48000):
    import numpy as np
    import soundfile as sf

    count = int(seconds * rate)
    if kind == "silence":
        wave = np.zeros(count, np.float32)
    elif kind == "clip":
        wave = np.ones(count, np.float32)
    elif kind == "noise":
        wave = np.random.default_rng(0).uniform(-0.2, 0.2, count).astype(np.float32)
    else:
        time = np.arange(count) / rate
        wave = (0.2 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, np.stack([wave, wave], axis=1), rate, subtype="FLOAT")


def _project(root, kinds=None, seconds=0.25, gain_file=False, post_file=False, skip=()):
    kinds = kinds or {}
    project = os.path.join(root, "job")
    source = os.path.join(root, "song.wav")
    mapping = {
        "source": source,
        "separator_input": os.path.join(project, ".work", "01_standard_input.wav"),
        "vocal_separated": os.path.join(project, "stems", "01_vocal_original.wav"),
        "instrumental": os.path.join(project, "stems", "04_instrumental.wav"),
        "vocal_preprocessed": os.path.join(project, "stems", "02_vocal_cleaned.wav"),
        "rvc_raw": os.path.join(project, "stems", "03_vocal_rvc_zhoujielun.wav"),
        "aligned": os.path.join(project, "stems", "03_vocal_aligned.wav"),
        "mixed": os.path.join(project, ".work", "06_mixed.wav"),
        "master": os.path.join(project, "master", "demo_MASTER.wav"),
    }
    if gain_file:
        mapping["rvc_input"] = os.path.join(project, ".work", "05_rvc_input.wav")
    if post_file:
        mapping["rvc_post"] = os.path.join(project, ".work", "07_rvc_post.wav")
    for key, path in mapping.items():
        if key in skip:
            continue
        _write_wav(path, kinds.get(key, "tone"), seconds.get(key, 0.25) if isinstance(seconds, dict) else seconds)
    return project, source


class StageQualityTraceTests(unittest.TestCase):
    def test_clean_project_has_no_objective_cause_and_keeps_existing_names(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root)
            first = build_stage_trace(project, source_path=source)
            second = build_stage_trace(project, source_path=source)
            self.assertEqual(first, second)
            self.assertFalse(first["blocks_job"])
            self.assertFalse(first["copies_audio"])
            self.assertEqual(first["listening_quality"], "not_assessed")
            self.assertEqual(first["quality_acceptance"], "NOT_ASSESSED")
            self.assertEqual(first["diagnosis"]["status"], "NO_OBJECTIVE_FLAG")
            self.assertIsNone(first["diagnosis"]["first_flagged_stage"])
            self.assertIsNone(first["diagnosis"]["rule"])
            by_id = {stage["id"]: stage for stage in first["stages"]}
            self.assertTrue(by_id["vocal_separated"]["path"].endswith("01_vocal_original.wav"))
            self.assertTrue(by_id["instrumental"]["path"].endswith("04_instrumental.wav"))
            self.assertEqual(by_id["rvc_input"]["same_path_as"], "vocal_preprocessed")
            self.assertEqual(by_id["rvc_input"]["resolved_from"], "cleaned_vocal")
            self.assertEqual(by_id["rvc_post"]["same_path_as"], "rvc_raw")
            self.assertEqual(by_id["rvc_post"]["resolved_from"], "rvc_raw")
            self.assertTrue(all(row["present"] for row in audition_rows(first)))
            self.assertNotIn("separate_audio", Path(ROOT, "core", "stage_quality_trace.py").read_text(encoding="utf-8"))
            self.assertNotIn("convert_voice", Path(ROOT, "core", "stage_quality_trace.py").read_text(encoding="utf-8"))

    def test_silence_at_separation_points_at_separator_not_rvc(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root, {"vocal_separated": "silence"})
            trace = build_stage_trace(project, source_path=source)
            diagnosis = trace["diagnosis"]
            self.assertEqual(diagnosis["status"], "OBJECTIVE_FLAG")
            self.assertEqual(diagnosis["first_flagged_stage"], "vocal_separated")
            self.assertEqual(diagnosis["rule"], "A")
            self.assertEqual(diagnosis["likely_cause"], "Separator")
            self.assertIn("RVC", diagnosis["do_not_tune"])
            self.assertIn("不要先调 RVC", diagnosis["conclusion"])
            self.assertEqual(diagnosis["listening_quality"], "not_assessed")
            self.assertTrue(any(item["kind"] == "silence_onset" for item in diagnosis["flags"]))

    def test_clipping_at_rvc_raw_points_at_rvc_not_mix(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root, {"rvc_raw": "clip"})
            diagnosis = build_stage_trace(project, source_path=source)["diagnosis"]
            self.assertEqual(diagnosis["first_flagged_stage"], "rvc_raw")
            self.assertEqual(diagnosis["rule"], "C")
            self.assertIn("F0", diagnosis["checks"])
            self.assertIn("Index", diagnosis["checks"])
            self.assertIn("Mix", diagnosis["do_not_tune"])
            self.assertEqual(diagnosis["quality_acceptance"], "NOT_ASSESSED")

    def test_short_master_points_at_master(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root, seconds=1.0)
            _write_wav(os.path.join(project, "master", "demo_MASTER.wav"), "tone", seconds=0.05)
            diagnosis = build_stage_trace(project, source_path=source)["diagnosis"]
            self.assertEqual(diagnosis["first_flagged_stage"], "master")
            self.assertEqual(diagnosis["rule"], "E")
            self.assertIn("Limiter", diagnosis["checks"])

    def test_mono_rvc_is_recorded_without_becoming_a_damage_rule(self):
        import soundfile as sf
        import numpy as np

        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root)
            raw = os.path.join(project, "stems", "03_vocal_rvc_zhoujielun.wav")
            count = int(0.25 * 48000)
            time = np.arange(count) / 48000
            wave = (0.2 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
            sf.write(raw, wave, 48000, subtype="FLOAT")
            diagnosis = build_stage_trace(project, source_path=source)["diagnosis"]
            self.assertEqual(diagnosis["status"], "NO_OBJECTIVE_FLAG")
            self.assertIsNone(diagnosis["rule"])
            self.assertTrue(any(item["kind"] == "channel_change" and item["stage_id"] == "rvc_raw" for item in diagnosis["observations"]))
            self.assertIn("声道从 2 变为 1", diagnosis["conclusion"])
            self.assertIn("不是听审结论", diagnosis["conclusion"])

    def test_high_frequency_jump_at_rvc_is_only_a_proxy(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root, {"rvc_raw": "noise"})
            diagnosis = build_stage_trace(project, source_path=source)["diagnosis"]
            self.assertEqual(diagnosis["first_flagged_stage"], "rvc_raw")
            self.assertEqual(diagnosis["rule"], "C")
            self.assertTrue(any(item["kind"] == "hf_ratio_jump" for item in diagnosis["flags"]))
            self.assertIn("不是听审", diagnosis["conclusion"])

    def test_duration_shift_names_the_reference_that_was_actually_read(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root, seconds={"source": 1.0, "master": 0.2}, skip={"separator_input"})
            diagnosis = build_stage_trace(project, source_path=source)["diagnosis"]
            shifts = [item for item in diagnosis["flags"] if item["kind"] == "duration_shift"]
            self.assertTrue(shifts)
            self.assertIn("原曲", shifts[0]["detail"])
            self.assertNotIn("分离实际输入", shifts[0]["detail"])
            present, present_source = _project(os.path.join(root, "both"), seconds=1.0)
            _write_wav(os.path.join(present, "master", "demo_MASTER.wav"), "tone", seconds=0.05)
            named = build_stage_trace(present, source_path=present_source)["diagnosis"]
            master_shift = [item for item in named["flags"] if item["stage_id"] == "master" and item["kind"] == "duration_shift"]
            self.assertEqual(len(master_shift), 1)
            self.assertIn("分离实际输入", master_shift[0]["detail"])

    def test_missing_rvc_does_not_invent_a_cause(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root, skip={"rvc_raw"})
            diagnosis = build_stage_trace(project, source_path=source)["diagnosis"]
            self.assertEqual(diagnosis["status"], "INCOMPLETE")
            self.assertIsNone(diagnosis["first_flagged_stage"])
            self.assertIsNone(diagnosis["rule"])
            self.assertIn("rvc_raw", diagnosis["missing_stages"])
            self.assertIn("rvc_post", diagnosis["missing_stages"])

    def test_listening_labels_use_the_rule_without_accepting_quality(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root)
            trace = build_stage_trace(project, source_path=source)
            marked = diagnose_stages(trace["stages"], {"source": "ok", "vocal_separated": "degraded"})
            self.assertEqual(marked["basis"], "supplied_listening_labels")
            self.assertEqual(marked["status"], "LISTENING_INCOMPLETE")
            self.assertIsNone(marked["rule"])
            self.assertEqual(marked["candidate_rule"], "A")
            self.assertEqual(marked["quality_acceptance"], "NOT_ASSESSED")
            self.assertEqual(marked["objective_status"], "NO_OBJECTIVE_FLAG")
            aliased = build_stage_trace(project)
            same_file = diagnose_stages(aliased["stages"], {"source": "ok", "vocal_separated": "degraded"})
            self.assertEqual(same_file["status"], "LISTENING_RULE")
            self.assertEqual(same_file["rule"], "A")
            inherited = diagnose_stages(trace["stages"], {"vocal_preprocessed": "ok", "rvc_raw": "degraded"})
            self.assertEqual(inherited["status"], "LISTENING_RULE")
            self.assertEqual(inherited["rule"], "C")
            incomplete = diagnose_stages(trace["stages"], {"vocal_separated": "degraded"})
            self.assertEqual(incomplete["status"], "LISTENING_INCOMPLETE")
            self.assertIsNone(incomplete["rule"])
            self.assertEqual(incomplete["candidate_rule"], "A")
            saved = write_listening_marks(project, {"rvc_input": "ok", "rvc_raw": "degraded"}, trace["stages"])
            self.assertEqual(saved["quality_acceptance"], "NOT_ASSESSED")
            self.assertEqual(saved["diagnosis"]["rule"], "C")
            with self.assertRaises(ValueError):
                diagnose_stages(trace["stages"], {"not_a_stage": "ok"})

    def test_report_and_distinct_gain_file_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="stage-trace-") as root:
            project, source = _project(root, gain_file=True, post_file=True)
            reports = os.path.join(project, "reports")
            os.makedirs(reports)
            report = os.path.join(reports, "process_report.md")
            report_file = open(report, "w", encoding="utf-8")
            report_file.write("# report\n")
            report_file.close()
            append_stage_trace(report, project, source_path=source)
            append_stage_trace(report, project, source_path=source)
            text = Path(report).read_text(encoding="utf-8")
            self.assertEqual(text.count(HEADING), 1)
            loaded = load_trace(os.path.join(reports, "stage_quality_trace.json"))
            by_id = {stage["id"]: stage for stage in loaded["stages"]}
            self.assertEqual(by_id["rvc_input"]["resolved_from"], "gain_file")
            self.assertIsNone(by_id["rvc_input"]["same_path_as"])
            self.assertEqual(by_id["rvc_post"]["resolved_from"], "post_file")
            loaded["diagnosis"]["quality_acceptance"] = "ACCEPTED"
            from core.stage_quality_trace import load_trace_data
            with self.assertRaises(ValueError):
                load_trace_data(loaded)

    def test_fingerprint_and_call_sites_stay_on_the_voice_report(self):
        root = Path(ROOT)
        self.assertEqual(stage_quality_trace_fingerprint_paths(root, {"mode": "polish"}), [])
        self.assertEqual(stage_quality_trace_fingerprint_paths(root, {}), [])
        voiced = stage_quality_trace_fingerprint_paths(root, {"mode": "voice_conversion"})
        self.assertEqual(voiced, [root / "core" / "stage_quality_trace.py"])
        pipeline = (root / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("append_stage_trace", pipeline)
        import ast
        calls = [node for node in ast.walk(ast.parse(pipeline)) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == '_separate_and_copy']
        self.assertTrue(any(isinstance(node.args[0], ast.Name) and node.args[0].id == 'processing_wav'
                            for node in calls))
        window = (root / "AI_Music_Processor.pyw").read_text(encoding="utf-8")
        self.assertIn("换声质量诊断", window)
        manifest = {"output_paths": {"stems_dir": os.path.join(root, "job", "stems")}}
        self.assertEqual(project_dir_from_manifest(manifest), os.path.join(root, "job"))

    def test_unused_dual_stems_can_be_opened_without_renaming_qc_stems(self):
        with tempfile.TemporaryDirectory() as folder:
            stems = os.path.join(folder, "stems")
            os.makedirs(stems)
            vocal = os.path.join(stems, "01_vocal_original.wav")
            with open(vocal, "wb") as handle:
                handle.write(b"vocal")
            unused = os.path.join(stems, "kim_instrumental.wav")
            with open(unused, "wb") as handle:
                handle.write(b"kim-inst")
            trace = build_stage_trace(folder)
            self.assertEqual([stage["id"] for stage in trace["stages"]], [
                "source", "separator_input", "vocal_separated", "instrumental", "vocal_preprocessed",
                "rvc_input", "rvc_raw", "rvc_post", "aligned", "mixed", "master",
            ])
            vocal = next(stage for stage in trace["stages"] if stage["id"] == "vocal_separated")
            self.assertTrue(vocal["path"].endswith("01_vocal_original.wav"))
            self.assertEqual(trace["quality_acceptance"], "NOT_ASSESSED")
            rows = {row["id"]: row for row in audition_rows(trace)}
            self.assertTrue(rows["vocal_separated"]["official"])
            self.assertTrue(rows["kim_instrumental"]["present"])
            self.assertFalse(rows["kim_instrumental"]["official"])
            self.assertEqual(rows["kim_instrumental"]["path"], os.path.abspath(unused))
            plain = os.path.join(folder, "plain")
            os.makedirs(plain)
            self.assertNotIn("extra_audition", build_stage_trace(plain))


if __name__ == "__main__":
    unittest.main()
