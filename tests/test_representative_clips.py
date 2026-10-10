import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.representative_clips import CLIP_LIMIT, select_clips


class RepresentativeClipTests(unittest.TestCase):
    def test_short_audio_returns_itself_and_long_clip_edges_repeat(self):
        import numpy as np

        sample_rate = 48000
        short = np.arange(1000, dtype=np.float64)
        returned = select_clips(short, sample_rate, window_seconds=8.0, limit=CLIP_LIMIT)
        self.assertIs(returned, short)
        long = np.arange(sample_rate * 20, dtype=np.float64)
        first = select_clips(long, sample_rate, window_seconds=2.0, limit=3)
        second = select_clips(long, sample_rate, window_seconds=2.0, limit=3)
        self.assertIsInstance(first, list)
        self.assertLessEqual(len(first), 3)
        self.assertGreaterEqual(len(first), 1)
        self.assertEqual(len(first), len(second))
        starts = []
        for left, right in zip(first, second):
            self.assertEqual(left["start_sample"], right["start_sample"])
            self.assertEqual(left["end_sample"], right["end_sample"])
            self.assertEqual(left["start_timecode"], right["start_timecode"])
            self.assertEqual(left["end_timecode"], right["end_timecode"])
            self.assertEqual(left["samples"][0], right["samples"][0])
            self.assertEqual(left["samples"][-1], right["samples"][-1])
            self.assertEqual(left["samples"][0], long[left["start_sample"]])
            self.assertEqual(left["samples"][-1], long[left["end_sample"] - 1])
            self.assertRegex(left["start_timecode"], r"^\d{2}:\d{2}:\d{2}\.\d{3}$")
            starts.append(left["start_sample"])
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), len(set(starts)))
        window = 2 * sample_rate
        self.assertTrue(all(item["end_sample"] - item["start_sample"] == window for item in first))
        self.assertLess(first[-1]["end_sample"], len(long) + 1)
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        self.assertTrue('from core.representative_clips import select_clips' in pipeline)
        self.assertTrue('window_seconds=8.0, limit=3' in pipeline)
        self.assertIn("spec['schema_version'] = 1", (ROOT / "core" / "job_spec.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
