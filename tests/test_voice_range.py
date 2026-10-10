import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import soundfile as sf
from core.voice_range import analyze_voice_range


class VoiceRangeTests(unittest.TestCase):
    def test_real_range_never_guesses_gender_or_applies_pitch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'voice.wav'
            time = np.arange(24000)/24000
            signal = (.2*np.sin(2*np.pi*220*time)+.1*np.sin(2*np.pi*440*time)).astype('float32')
            sf.write(path, signal, 24000, subtype='FLOAT')
            mixed = analyze_voice_range(path)
            self.assertEqual(mixed['suggestion']['reason'], 'mixed_music_is_not_a_vocal_range')
            vocal = analyze_voice_range(path, source_kind='isolated_vocal')
            self.assertAlmostEqual(vocal['observed_range_hz']['median_hz'], 220, delta=2)
            self.assertEqual(vocal['gender'], 'NOT_INFERRED')
            self.assertEqual(vocal['suggestion']['reason'], 'model_training_range_unknown')
            target = {'low_hz': 300, 'median_hz': 350, 'high_hz': 450, 'provenance': 'user_declared_range'}
            suggested = analyze_voice_range(path, 'explicit_vocal_only', target)
            self.assertGreater(suggested['suggestion']['semitones'], 0)
            self.assertFalse(suggested['suggestion']['applied'])
            unchanged, _ = sf.read(path, dtype='float32')
            np.testing.assert_array_equal(signal, unchanged)

    def test_bounded_nonoverlapping_windows_silence_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'silence.wav'
            sf.write(path, np.zeros(24000*35, dtype='float32'), 24000, subtype='FLOAT')
            result = analyze_voice_range(path, 'isolated_vocal')
            self.assertLessEqual(result['sampled_seconds'], 24)
            self.assertEqual(len(result['clips']), 3)
            self.assertIsNone(result['observed_range_hz'])
            self.assertIsNone(result['suggestion']['semitones'])
            def cancel():
                raise RuntimeError('cancelled')
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                analyze_voice_range(path, cancel=cancel)


if __name__ == '__main__':
    unittest.main()
