import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import soundfile as sf
from core.polish_policy import default_policy, reference_identity, tone_parameters
from core.polish_reference_runtime import prepare_reference
from core.storage import stage_cache_key


class ReferenceRuntimeTests(unittest.TestCase):
    def test_real_reference_read_failure_bypasses_without_mutating_snapshot_or_cache_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'reference.wav')
            sf.write(path, np.column_stack([np.sin(np.arange(4800)*.1)*.1]*2), 48000, subtype='FLOAT')
            identity = reference_identity(path)
            policy = default_policy(); policy['reference'].update(identity)
            original = copy.deepcopy(policy)
            effective, audio, rate = prepare_reference(policy, identity)
            self.assertIsNotNone(audio)
            with patch('soundfile.read', side_effect=RuntimeError('real read failed')):
                bypass, audio, rate = prepare_reference(policy, identity)
            self.assertFalse(bypass['reference']['enabled'])
            self.assertIn('read_error', bypass['reference'])
            self.assertEqual(original, policy)
            self.assertNotEqual(stage_cache_key('Tone', {}, tone_parameters(effective, identity)),
                                stage_cache_key('Tone', {}, tone_parameters(bypass, identity)))
            Path(path).write_bytes(b'changed')
            with self.assertRaises(ValueError): prepare_reference(policy, identity)

    def test_decode_identity_change_and_invalid_analysis_never_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'reference.wav')
            sf.write(path, np.ones((4800,2))*.1, 48000, subtype='FLOAT')
            identity=reference_identity(path); policy=default_policy();policy['reference'].update(identity)
            def changing(source, **kwargs):
                Path(source).write_bytes(b'changed');raise RuntimeError('decoder failure')
            with self.assertRaises(ValueError): prepare_reference(policy, identity, changing)
            sf.write(path, np.ones((4800,2))*.1, 48000, subtype='FLOAT')
            identity=reference_identity(path);identity['analysis_fingerprint']='invalid'
            with self.assertRaises(ValueError): prepare_reference(policy, identity)
