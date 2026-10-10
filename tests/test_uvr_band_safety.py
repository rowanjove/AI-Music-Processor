import types
import unittest
import numpy as np
from core.uvr_band_safety import install_zero_band_buffers


def combine():
    return np.ndarray(shape=(2, 9, 3), dtype=complex)


def path_audio(self):
    return spec_utils.cmb_spectrogram_to_wave()


class UvrBandSafetyTests(unittest.TestCase):
    def test_zero_initialization_is_worker_instance_local(self):
        class ToxicNumpy:
            def ndarray(self, shape, dtype):
                return np.full(shape, complex(np.nan), dtype=dtype)
            def __getattr__(self, key):
                return getattr(np, key)
        old_combine=types.FunctionType(combine.__code__, {'np':ToxicNumpy(), '__builtins__':__builtins__})
        module=types.SimpleNamespace(cmb_spectrogram_to_wave=old_combine)
        method=types.FunctionType(path_audio.__code__, {'spec_utils':module})
        class Instance:
            _path_audio_=method
        safe=Instance();untouched=Instance()
        self.assertTrue(np.isnan(safe._path_audio_()).all())
        install_zero_band_buffers(safe)
        self.assertTrue((safe._path_audio_()==0).all())
        self.assertTrue(np.isnan(untouched._path_audio_()).all())
        self.assertIs(module.cmb_spectrogram_to_wave, old_combine)
