import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache_fingerprint import f0_quality_fingerprint_paths
from core.f0_quality import (
    EXPORT_REASON,
    STATUS_STABLE,
    STATUS_UNOBSERVABLE,
    STATUS_UNSTABLE,
    append_f0_report,
    assess_f0_track,
    worker_export_record,
)
from core.qc import run_quality_control

RECORD = ROOT / "tests" / "fixtures" / "f0_quality_vcq011.json"


def _unstable():
    contour = []
    for index in range(8):
        contour.append(110.0 if index % 2 == 0 else 440.0)
    return contour


class F0QualityTests(unittest.TestCase):
    def test_synthetic_contours_are_explainable_and_do_not_score(self):
        expected = json.loads(RECORD.read_text(encoding="utf-8"))
        stable = assess_f0_track([220.0] * 32)
        unstable = assess_f0_track(_unstable())
        missing = worker_export_record()
        self.assertEqual(expected["listening_quality"], "not_assessed")
        self.assertIsNone(expected["score"])
        self.assertFalse(expected["blocks_job"])
        for record in (stable, unstable, missing):
            self.assertFalse(record["blocks_job"])
            self.assertIsNone(record["score"])
        self.assertEqual(stable["status"], STATUS_STABLE)
        self.assertEqual(stable["frames"], expected["stable_220hz"]["frames"])
        self.assertEqual(stable["jump_count"], expected["stable_220hz"]["jump_count"])
        self.assertEqual(stable["voiced_ratio"], expected["stable_220hz"]["voiced_ratio"])
        self.assertEqual(unstable["status"], expected["unstable_110_440"]["status"])
        self.assertEqual(unstable["status"], STATUS_UNSTABLE)
        self.assertEqual(unstable["jump_count"], expected["unstable_110_440"]["jump_count"])
        self.assertIn("12", unstable["reason"])
        self.assertEqual(missing["status"], STATUS_UNOBSERVABLE)
        self.assertEqual(missing["status"], expected["song_f0"])
        self.assertEqual(missing["reason"], EXPORT_REASON)
        self.assertEqual(missing["reason"], expected["export_reason"])
        self.assertIsNone(missing["frames"])
        gap = assess_f0_track([220.0, 0.0, 220.0])
        self.assertEqual(gap["status"], STATUS_STABLE)
        self.assertEqual(gap["jump_count"], 0)
        low = assess_f0_track([40.0, 40.0])
        self.assertEqual(low["status"], STATUS_UNSTABLE)
        self.assertIn("50", low["reason"])
        empty = assess_f0_track([])
        self.assertEqual(empty["status"], STATUS_UNSTABLE)
        self.assertFalse(empty["blocks_job"])
        bad = assess_f0_track([220.0, float("nan")])
        self.assertEqual(bad["status"], STATUS_UNSTABLE)
        self.assertIsNone(bad["score"])
        self.assertEqual(assess_f0_track("missing.wav")["reason"], "没有可检查的 F0 轨迹")

    def test_report_records_the_gate_without_failing_the_master(self):
        import numpy as np
        import soundfile as sf

        tone = np.zeros(48000, np.float32)
        tone[:240] = np.float32(0.2)
        unstable = assess_f0_track(_unstable())
        with tempfile.TemporaryDirectory(prefix="f0-gate-") as folder:
            master = os.path.join(folder, "master.wav")
            sf.write(master, tone, 48000, subtype="FLOAT")
            data, report = run_quality_control(master, None, "fixture", "polish")
            self.assertNotIn("f0_quality", data)
            self.assertNotIn("## F0 轨迹", report)
            report_path = os.path.join(folder, "process_report.md")
            with open(report_path, "w", encoding="utf-8") as handle:
                handle.write(report)
            append_f0_report(report_path, unstable)
            append_f0_report(report_path, worker_export_record())
            with open(report_path, encoding="utf-8") as handle:
                written = handle.read()
        self.assertNotEqual(data["status"], "FAIL")
        self.assertFalse(any("F0" in item for item in data["failures"]))
        self.assertEqual(written.count("## F0 轨迹"), 1)
        self.assertIn(STATUS_UNSTABLE, written)
        self.assertIn("不改变任务成败：是", written)
        self.assertIn("分数：无", written)

    def test_worker_does_not_export_f0_and_cache_stays_narrow(self):
        worker = (ROOT / "core" / "rvc_worker.py").read_text(encoding="utf-8")
        vendor = (ROOT / "tools" / "RVC" / "RVC-beta-v2-0618" / "vc_infer_pipeline.py").read_text(encoding="utf-8")
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        module = (ROOT / "core" / "f0_quality.py").read_text(encoding="utf-8")
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertNotIn("np.save", worker)
        self.assertNotIn(".f0-qc.json", worker)
        self.assertNotIn("rmvpe", vendor)
        self.assertIn("worker_export_record", pipeline)
        self.assertLess(pipeline.index(".f0-qc.json"), pipeline.index("self._align"))
        self.assertNotIn("import soundfile", module)
        self.assertNotIn("import pyworld", module)
        self.assertNotIn("import torch", module)
        self.assertNotIn("pyworld.", module)
        root = Path("workspace")
        self.assertEqual(f0_quality_fingerprint_paths(root, {"mode": "polish"}), [])
        self.assertEqual(f0_quality_fingerprint_paths(root, {"mode": "transcribe"}), [])
        self.assertEqual(
            f0_quality_fingerprint_paths(root, {"mode": "voice_conversion"}),
            [root / "core" / "f0_quality.py"],
        )
        self.assertIn("stage_contract_version=2", fingerprint)
        self.assertNotIn("stage_contract_version=3", fingerprint)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))
        stored = RECORD.read_text(encoding="utf-8")
        self.assertNotIn("improved", stored)


if __name__ == "__main__":
    unittest.main()
