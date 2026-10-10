import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import FFMPEG_EXE
from core.polish_policy import decode_reference, load_frozen_reference, reference_identity


def _make_m4a(folder):
    folder = Path(folder)
    rate = 44100
    time = np.arange(rate, dtype=np.float64) / rate
    samples = np.column_stack((0.2 * np.sin(2 * np.pi * 440 * time),
                               0.15 * np.sin(2 * np.pi * 880 * time)))
    wav = folder / "reference-source.wav"
    m4a = folder / "reference.m4a"
    sf.write(wav, samples.astype(np.float32), rate, subtype="FLOAT")
    subprocess.run([FFMPEG_EXE, "-y", "-v", "error", "-i", str(wav),
                    "-c:a", "aac", "-b:a", "128k", str(m4a)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return m4a


class PolishReferenceDecodeTests(unittest.TestCase):
    def test_real_m4a_freezes_and_loads_decoded_float_identity(self):
        with tempfile.TemporaryDirectory(prefix="polish-reference-") as folder:
            m4a = _make_m4a(folder)
            identity = reference_identity(str(m4a))
            audio, rate = load_frozen_reference(identity)
            self.assertEqual(rate, 48000)
            self.assertEqual(audio.shape[1], 2)
            self.assertGreater(audio.shape[0], 0)
            self.assertTrue(np.isfinite(audio).all())
            self.assertAlmostEqual(identity["duration"], len(audio) / rate, places=9)
            self.assertTrue(identity["analysis_fingerprint"])

            raw_audio, raw_rate = decode_reference(str(m4a), expected_sha256=identity["sha256"])
            self.assertEqual(raw_rate, 48000)
            self.assertTrue(np.array_equal(audio, raw_audio))

    def test_reference_changes_are_rejected_before_or_during_decode(self):
        with tempfile.TemporaryDirectory(prefix="polish-reference-change-") as folder:
            m4a = _make_m4a(folder)
            identity = reference_identity(str(m4a))
            with open(m4a, "ab") as handle:
                handle.write(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                load_frozen_reference(identity)

        with tempfile.TemporaryDirectory(prefix="polish-reference-race-") as folder:
            m4a = _make_m4a(folder)
            original = reference_identity(str(m4a))
            from core.analyzer import standardize_to_48k_float

            def decode_then_mutate(source, destination):
                standardize_to_48k_float(source, destination)
                with open(source, "ab") as handle:
                    handle.write(b"changed while decoding")

            with patch("core.analyzer.standardize_to_48k_float", side_effect=decode_then_mutate):
                with self.assertRaisesRegex(ValueError, "解码期间发生变化"):
                    decode_reference(str(m4a), expected_sha256=original["sha256"])

    def test_file_polish_uses_same_frozen_reference_loader(self):
        with tempfile.TemporaryDirectory(prefix="polish-reference-chain-") as folder:
            folder = Path(folder)
            m4a = _make_m4a(folder)
            source = folder / "source.wav"
            sf.write(source, np.zeros((4800, 2), dtype=np.float32), 48000, subtype="FLOAT")
            from core import polish_chain

            stage_calls = []

            def copy_stage(kind, source_wav, dest_wav, report, policy, reference=None, reference_rate=None):
                stage_calls.append((kind, reference, reference_rate))
                Path(dest_wav).parent.mkdir(parents=True, exist_ok=True)
                Path(dest_wav).write_bytes(Path(source_wav).read_bytes())
                return {"wav": dest_wav, "decision_path": dest_wav + ".decision.json"}

            with patch("core.polish_chain.analyze_polish_buffer", return_value={}), \
                 patch("core.polish_chain.render_stage", side_effect=copy_stage), \
                 patch("core.polish_policy.load_frozen_reference", wraps=load_frozen_reference) as load:
                polish_chain.render_polish_files(str(source), str(folder / "result"), reference_path=str(m4a))

            self.assertTrue(load.called)
            tone = next(item for item in stage_calls if item[0] == "tone")
            self.assertEqual(tone[2], 48000)
            self.assertEqual(tone[1].shape[1], 2)


if __name__ == "__main__":
    unittest.main()
