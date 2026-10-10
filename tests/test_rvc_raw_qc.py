import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.qc import inspect_rvc_raw, run_quality_control

RVC_WAV = (
    ROOT / "output" / "acceptance" / "audit_repair"
    / "验收 中文短样本_20261004_192142_144337" / "stems" / "03_vocal_rvc_zhoujielun.wav"
)
BEFORE = ROOT / "tests" / "fixtures" / "voice_chain_before.json"


class RvcRawQcTests(unittest.TestCase):
    def test_silent_clipped_and_normal_float_are_recorded(self):
        with tempfile.TemporaryDirectory(prefix="rvc-raw-") as folder:
            normal = os.path.join(folder, "normal.wav")
            clipped = os.path.join(folder, "clipped.wav")
            silent = os.path.join(folder, "silent.wav")
            invalid = os.path.join(folder, "nan.wav")
            normal_audio = np.zeros(480, np.float32)
            normal_audio[0] = np.float32(0.25)
            clipped_audio = np.zeros(480, np.float32)
            clipped_audio[0] = np.float32(1.0)
            clipped_audio[1] = np.float32(1.25)
            invalid_audio = np.zeros(480, np.float32)
            invalid_audio[0] = np.float32(np.nan)
            sf.write(normal, normal_audio, 48000, subtype="FLOAT")
            sf.write(clipped, clipped_audio, 48000, subtype="FLOAT")
            sf.write(silent, np.zeros(480, np.float32), 48000, subtype="FLOAT")
            sf.write(invalid, invalid_audio, 48000, subtype="FLOAT")
            normal_record = inspect_rvc_raw(normal)
            self.assertEqual(inspect_rvc_raw(normal), normal_record)
            clipped_record = inspect_rvc_raw(clipped)
            silent_record = inspect_rvc_raw(silent)
            invalid_record = inspect_rvc_raw(invalid)
        self.assertEqual(normal_record["sample_peak"], 0.25)
        self.assertEqual(normal_record["clipped_samples"], 0)
        self.assertFalse(normal_record["silent"])
        self.assertTrue(normal_record["finite"])
        self.assertEqual(normal_record["findings"], [])
        self.assertFalse(normal_record["blocks_job"])
        self.assertGreater(clipped_record["clipped_samples"], 0)
        self.assertIn("RVC 原始输出已削波", clipped_record["findings"])
        self.assertFalse(clipped_record["blocks_job"])
        self.assertTrue(silent_record["silent"])
        self.assertIn("RVC 原始输出静音", silent_record["findings"])
        self.assertFalse(silent_record["blocks_job"])
        self.assertFalse(invalid_record["finite"])
        self.assertTrue(invalid_record["findings"])
        self.assertFalse(invalid_record["blocks_job"])

    def test_report_separates_clean_master_from_clipped_rvc(self):
        tone = np.zeros(48000, np.float32)
        tone[:240] = np.float32(0.2)
        with tempfile.TemporaryDirectory(prefix="rvc-raw-report-") as folder:
            master = os.path.join(folder, "master.wav")
            clipped = os.path.join(folder, "03_vocal_rvc_fixture.wav")
            sf.write(master, tone, 48000, subtype="FLOAT")
            clipped_audio = np.zeros(480, np.float32)
            clipped_audio[0] = np.float32(1.0)
            sf.write(clipped, clipped_audio, 48000, subtype="FLOAT")
            raw = inspect_rvc_raw(clipped)
            data, report = run_quality_control(master, None, "fixture", "polish", rvc_raw=raw)
        self.assertEqual(data["clipped_samples"], 0)
        self.assertNotIn("成品存在削波采样", data["failures"])
        self.assertGreater(data["rvc_raw"]["clipped_samples"], 0)
        self.assertFalse(data["rvc_raw"]["blocks_job"])
        self.assertNotEqual(data["status"], "FAIL")
        self.assertIn("母带削波采样：0", report)
        self.assertIn("RVC 原始输出已削波", report)
        self.assertIn("不改变任务成败：是", report)

    def test_existing_short_rvc_output_matches_before_peak(self):
        self.assertTrue(RVC_WAV.is_file(), "短样本的 RVC 输出不在磁盘上")
        before = json.loads(BEFORE.read_text(encoding="utf-8"))
        record = inspect_rvc_raw(str(RVC_WAV))
        saved = before["points"]["rvc_output"]
        self.assertEqual(record["sample_peak"], saved["sample_peak"])
        self.assertEqual(record["clipped_samples"], saved["clipped_samples"])
        self.assertEqual(record["channel_rms"], saved["channel_rms"])
        self.assertFalse(record["silent"])
        self.assertTrue(record["finite"])
        self.assertFalse(record["blocks_job"])
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertLess(pipeline.index("inspect_rvc_raw"), pipeline.index("self._align"))
        self.assertIn(".raw-qc.json", pipeline)


if __name__ == "__main__":
    unittest.main()
