import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.artifact_score import (
    HEADING,
    LABEL,
    append_artifact_report,
    assess_artifact,
    format_artifact_report,
)
from core.cache_fingerprint import artifact_score_fingerprint_paths


def _tone(clipped):
    import numpy as np

    sample_rate = 48000
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    wave = np.sin(2.0 * math.pi * 220.0 * time)
    if clipped:
        return np.clip(wave * 3.0, -1.0, 1.0), sample_rate
    return wave * 0.4, sample_rate


class ArtifactScoreTests(unittest.TestCase):
    def test_clipped_silent_and_normal_stay_heuristic(self):
        normal, sample_rate = _tone(False)
        clipped, _rate = _tone(True)
        import numpy as np

        silent = np.zeros(sample_rate, dtype=np.float64)
        records = {
            "normal": assess_artifact(normal, sample_rate),
            "clipped": assess_artifact(clipped, sample_rate),
            "silent": assess_artifact(silent, sample_rate),
        }
        for record in records.values():
            self.assertFalse(record["quality_truth"])
            self.assertTrue(record["heuristic"])
            self.assertEqual(record["listening_quality"], "not_assessed")
            self.assertFalse(record["blocks_job"])
            self.assertEqual(record["label"], LABEL)
            self.assertIn("不是音质真值", record["label"])
        self.assertFalse(records["normal"]["silent"])
        self.assertFalse(records["clipped"]["silent"])
        self.assertTrue(records["silent"]["silent"])
        self.assertLess(records["silent"]["rms"], records["normal"]["rms"])
        self.assertGreater(records["clipped"]["clip_ratio"], records["normal"]["clip_ratio"])
        self.assertEqual(records["normal"]["clip_ratio"], 0.0)
        self.assertGreater(records["clipped"]["spectral_flatness"], records["normal"]["spectral_flatness"])
        self.assertGreater(records["clipped"]["high_frequency_ratio"], records["normal"]["high_frequency_ratio"])
        self.assertIsNone(records["silent"]["crest_factor"])
        self.assertGreater(records["normal"]["crest_factor"], records["clipped"]["crest_factor"])
        report = format_artifact_report(records["clipped"])
        self.assertIn(HEADING, report)
        self.assertIn("音质真值：否", report)
        self.assertIn("not_assessed", report)
        self.assertNotIn("improved", report)

    def test_report_appends_once_and_cache_stays_on_voice_qc(self):
        normal, sample_rate = _tone(False)
        record = assess_artifact(normal, sample_rate)
        with tempfile.TemporaryDirectory(prefix="artifact-score-") as folder:
            report_path = os.path.join(folder, "process_report.md")
            with open(report_path, "w", encoding="utf-8") as handle:
                handle.write("# 报告\n")
            append_artifact_report(report_path, record)
            append_artifact_report(report_path, record)
            with open(report_path, encoding="utf-8") as handle:
                written = handle.read()
        self.assertEqual(written.count(HEADING), 1)
        self.assertIn("不是音质真值", written)
        root = Path("workspace")
        self.assertEqual(artifact_score_fingerprint_paths(root, {"mode": "polish"}), [])
        self.assertEqual(artifact_score_fingerprint_paths(root, {"mode": "transcribe"}), [])
        self.assertEqual(
            artifact_score_fingerprint_paths(root, {"mode": "voice_conversion"}),
            [root / "core" / "artifact_score.py"],
        )
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        qc = (ROOT / "core" / "qc.py").read_text(encoding="utf-8")
        stages = (ROOT / "core" / "stage_operations.py").read_text(encoding="utf-8")
        fingerprint = (ROOT / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("assess_artifact_file", pipeline)
        self.assertIn("append_artifact_report", pipeline)
        self.assertNotIn(HEADING, qc)
        self.assertNotIn("artifact_score", stages)
        self.assertIn("stage_contract_version=2", fingerprint)
        self.assertNotIn("stage_contract_version=3", fingerprint)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))
        module = (ROOT / "core" / "artifact_score.py").read_text(encoding="utf-8")
        self.assertNotIn("import torch", module)
        self.assertNotIn("import pyworld", module)


if __name__ == "__main__":
    unittest.main()
