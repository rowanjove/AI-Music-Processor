import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.release_gate import _compare_unseeded_audio, _assert_unseeded_worker, compare_golden


class UnseededEngineeringPolicyTests(unittest.TestCase):
    def test_lower_stochastic_peak_does_not_erase_strict_failure(self):
        before = dict(sample_rate=48000, channels=2, duration=15., finite=True,
                      sample_peak=.53, clipped_samples=0, channel_rms=[.06, .06])
        after = dict(before, sample_peak=.49, channel_rms=[.0595, .0605])
        self.assertEqual(compare_golden({'peak': .53, 'duration': 15., 'clipped_samples': 0},
                                        {'peak': .49, 'duration': 15., 'clipped_samples': 0})['status'], 'FAIL')
        result = _compare_unseeded_audio(before, after)
        self.assertLess(result['peak_delta'], 0)
        for changes in (
                {'sample_peak': .541}, {'clipped_samples': 1}, {'duration': 14.9},
                {'finite': False}, {'sample_peak': float('nan')},
                {'channel_rms': [.0613, .06]}, {'channel_rms': [.0587, .06]},
                {'channel_rms': [0., 0.]}, {'sample_rate': 44100}, {'channels': 1},
                {'channel_rms': [float('inf'), .06]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _compare_unseeded_audio(before, dict(after, **changes))
        with self.assertRaises(ValueError):
            _compare_unseeded_audio(dict(before, channel_rms=[0., 0.]), dict(after, channel_rms=[0., 0.]))

    def test_missing_seed_receipt_cannot_masquerade_as_explicit_unseeded_execution(self):
        worker = {'terminal_worker_result': {'worker': 'rvc', 'status': 'complete',
                  'validation_seed': None, 'output_precision': 'legacy_int16', 'actual_f0': 'harvest',
                  'model_guard': {'complete_weight_shapes_verified': True}}}
        spec = {'f0_method': 'harvest'}
        _assert_unseeded_worker(worker, spec, {'worker': worker})
        for changes in ({'validation_seed': 0}, {'validation_seed': 42},
                        {'output_precision': 'float32'}, {'actual_f0': 'rmvpe'},
                        {'model_guard': {}}, {'status': 'failed'}):
            changed = copy.deepcopy(worker)
            changed['terminal_worker_result'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _assert_unseeded_worker(changed, spec, {'worker': changed})
        missing = copy.deepcopy(worker)
        missing['terminal_worker_result'].pop('validation_seed')
        with self.assertRaises(ValueError):
            _assert_unseeded_worker(missing, spec, {'worker': missing})
        with self.assertRaises(ValueError):
            _assert_unseeded_worker(worker, spec, {})


if __name__ == '__main__':
    unittest.main()
