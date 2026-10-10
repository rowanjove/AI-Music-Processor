import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.voice_quality_flow import apply_voice_quality, completed_choice, write_quality_sidecars


class QualityChoiceIdentityTests(unittest.TestCase):
    def test_reuse_rejects_changed_audio_and_frozen_parameters(self):
        with tempfile.TemporaryDirectory() as folder:
            primary = Path(folder) / "primary.wav"
            primary.write_bytes(b"primary")
            alternate = Path(folder) / "alternate.wav"
            alternate.write_bytes(b"alternate")
            parameters = {"quality_assist": True, "quality_retry_limit": 1}
            record = apply_voice_quality(parameters, {"pitch_shift": 0, "index_rate": .65,
                "effective_f0": {"requested": "harvest", "executed": "harvest"}},
                {"index_rate": .5}, str(primary), {"clip_ratio": .1}, None,
                infer=lambda payload: {"failed": False, "artifact": str(alternate)})
            write_quality_sidecars(str(primary), record)
            self.assertIsNotNone(completed_choice(str(primary), parameters))
            self.assertIsNone(completed_choice(str(primary), dict(parameters, index_rate=.4)))
            alternate.write_bytes(b"changed")
            self.assertIsNone(completed_choice(str(primary), parameters))
            alternate.write_bytes(b"alternate")
            primary.write_bytes(b"changed-primary")
            self.assertIsNone(completed_choice(str(primary), parameters))


if __name__ == "__main__":
    unittest.main()
