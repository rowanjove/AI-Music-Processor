import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.eta_estimator import estimate_remaining

IDENTITY = {'backend': 'rvc-0618-rmvpe', 'hardware': 'GTX1060', 'model_sha256': 'a'*64}


def observation(seconds, **extra):
    row = {'status': 'DONE', 'mode': 'voice_conversion', 'stage_name': 'RVC',
           'audio_duration': 15., 'started_at': 100., 'finished_at': 100.+seconds,
           'execution_kind': 'executed', 'identity': IDENTITY}
    row.update(extra)
    return row


class ETATests(unittest.TestCase):
    def test_only_matching_executed_measurements_estimate_current_stage(self):
        rows = [observation(12), observation(18), observation(100, cache_hit=True),
                observation(90, identity=dict(IDENTITY, hardware='CPU')),
                observation(120, execution_kind='cached'), observation(999, status='FAILED')]
        result = estimate_remaining(rows, 15, 'voice_conversion', 'RVC', IDENTITY, elapsed_seconds=5)
        self.assertEqual(result['sample_count'], 2)
        self.assertEqual(result['seconds'], 10)
        self.assertEqual(result['scope'], 'current_stage')
        self.assertLessEqual(result['range_seconds'][0], result['seconds'])
        self.assertGreaterEqual(result['range_seconds'][1], result['seconds'])

    def test_unknown_cached_only_overrun_and_invalid_input_never_fake_eta(self):
        for rows in ([], [observation(10)], [observation(10, reused=True)]*3,
                     [observation(10, audio_duration=500)]*3,
                     [observation(10, paused=True)]*3):
            self.assertIsNone(estimate_remaining(rows, 15, 'voice_conversion', 'RVC', IDENTITY)['seconds'])
        overrun = estimate_remaining([observation(10)]*2, 15, 'voice_conversion', 'RVC', IDENTITY, elapsed_seconds=12)
        self.assertIsNone(overrun['seconds'])
        self.assertEqual(overrun['basis'], 'running_longer_than_observed_median')
        self.assertEqual(estimate_remaining([], 15, 'voice_conversion', 'RVC')['basis'], 'execution_identity_unknown')
        for value in (0, -1, float('nan'), True):
            with self.assertRaises(ValueError):
                estimate_remaining([], value, 'polish', 'Tone')


if __name__ == '__main__':
    unittest.main()
