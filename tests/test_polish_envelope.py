import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from core.polish_dsp import _envelope, _envelope_python


class PolishEnvelopeTests(unittest.TestCase):
    def test_compiled_attack_release_matches_original_recurrence_exactly(self):
        generator = np.random.RandomState(91)
        for dtype in (np.float32, np.float64):
            fixtures = [np.zeros(0, dtype=dtype), np.zeros(500, dtype=dtype),
                        np.array([0, 1, .2, .1, .8, 0, 0], dtype=dtype),
                        np.abs(generator.normal(size=8000)).astype(dtype)]
            for samples in fixtures:
                for attack, release in ((.4, .05), (.9979, .9997), (0., 0.), (1., 1.)):
                    with self.subTest(dtype=dtype, size=samples.size, attack=attack):
                        expected = _envelope_python(samples, attack, release)
                        actual = _envelope(samples, attack, release)
                        self.assertEqual(actual.dtype, expected.dtype)
                        np.testing.assert_array_equal(actual, expected)


if __name__ == '__main__':
    unittest.main()
