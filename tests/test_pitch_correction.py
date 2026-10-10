import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import soundfile as sf
from core.pitch_correction import correct_pitch_file, _detect_scale, _observe, _notes, _scale


def harmonic_note(rate, duration, cents=0, vibrato=0):
    time = np.arange(int(rate * duration)) / rate
    frequency = 220 * 2 ** ((cents + vibrato * np.sin(2 * np.pi * 6 * time)) / 1200)
    phase = 2 * np.pi * np.cumsum(frequency) / rate
    return (.25 * np.sin(phase) + .1 * np.sin(2 * phase) + .05 * np.sin(3 * phase)).astype(np.float32)


class PitchCorrectionTests(unittest.TestCase):
    def test_default_off_preserves_samples_without_f0_import(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.wav', Path(directory)/'out.wav'
            samples = np.array([[.123456, -.235678], [0, .4]], dtype=np.float32)
            sf.write(source, samples, 8000, subtype='FLOAT')
            with mock.patch('core.pitch_correction._observe', side_effect=AssertionError('off cannot analyze')):
                result = correct_pitch_file(source, dest)
            actual, rate = sf.read(dest, dtype='float32', always_2d=True)
            np.testing.assert_array_equal(actual, samples)
            self.assertEqual(rate, 8000)
            self.assertEqual(result['report']['reason'], 'disabled')
            self.assertEqual(json.loads(Path(result['decision']).read_text())['status'], 'BYPASSED')

    def test_real_shift_reduces_detuning_preserves_duration_and_vibrato(self):
        rate = 24000
        samples = harmonic_note(rate, 1.5, cents=32, vibrato=12)
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.wav', Path(directory)/'out.wav'
            sf.write(source, samples, rate, subtype='FLOAT')
            result = correct_pitch_file(source, dest, mode='strong', key='A minor')
            actual, actual_rate = sf.read(dest, dtype='float32')
            self.assertEqual((len(actual), actual_rate), (len(samples), rate))
            self.assertEqual(result['report']['status'], 'APPLIED')
            self.assertTrue(np.isfinite(actual).all())
            self.assertLess(float(np.max(np.abs(actual))), 1.)
            before = _observe(samples, rate)[2]
            after = _observe(actual, rate)[2]
            before = before[20:-20]; after = after[20:-20]
            before_error = abs(float(np.nanmedian(before)) - 57)
            after_error = abs(float(np.nanmedian(after)) - 57)
            self.assertLess(after_error, before_error * .5)
            # Real modulation survives: no flattening to a single MIDI pitch.
            self.assertGreater(float(np.nanpercentile(after, 90) - np.nanpercentile(after, 10)), .08)
            self.assertEqual(result['report']['listening_quality'], 'READY_FOR_EXTERNAL_VALIDATION')

    def test_uncertain_auto_key_and_silence_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.wav', Path(directory)/'out.wav'
            for samples in (np.zeros(24000, dtype=np.float32), harmonic_note(24000, 1, cents=30)):
                sf.write(source, samples, 24000, subtype='FLOAT')
                result = correct_pitch_file(source, dest, mode='medium')
                self.assertEqual(result['report']['status'], 'BYPASSED')
                actual, _ = sf.read(dest, dtype='float32')
                np.testing.assert_array_equal(actual, samples)

    def test_scale_detection_glide_and_natural_deviation_policy(self):
        midi = np.repeat(np.array([60, 62, 64, 65, 67, 69, 71]) + .28, 40)
        scale, confidence = _detect_scale(midi)
        self.assertEqual(scale['pitch_classes'], [0, 2, 4, 5, 7, 9, 11])
        self.assertGreater(confidence, .5)
        self.assertIsNone(_detect_scale(np.repeat(57.3, 200))[0])
        times = np.arange(100) * .01
        for observed in (np.full(100, 57.1), 57.2 + .4 * times):
            self.assertEqual(_notes(times, observed, np.ones(100), _scale('A minor'), 'strong'), [])
        self.assertEqual(_scale('Bb minor')['key'], 'A# minor')

    def test_invalid_input_and_cancel_never_replace_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.wav', Path(directory)/'out.wav'
            sf.write(source, np.array([np.nan] * 24000), 24000, subtype='FLOAT')
            dest.write_bytes(b'preserved')
            with self.assertRaises(ValueError):
                correct_pitch_file(source, dest, mode='medium', key='C major')
            self.assertEqual(dest.read_bytes(), b'preserved')
            with self.assertRaises(ValueError):
                correct_pitch_file(source, dest, key='invalid scale')
            sf.write(source, harmonic_note(24000, 1), 24000, subtype='FLOAT')
            with self.assertRaises(ValueError):
                correct_pitch_file(source, source)
            def cancelled():
                raise RuntimeError('cancelled')
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                correct_pitch_file(source, dest, cancel=cancelled)
            self.assertEqual(dest.read_bytes(), b'preserved')


if __name__ == '__main__':
    unittest.main()
