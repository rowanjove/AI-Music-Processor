import types
import unittest

import numpy as np

from core.uvr_exact_length import BandLengthTracker, ExactLengthError, build_exact_length_spec_proxy


class _FakeLibrosa:
    def __init__(self, resample_lengths=None):
        self.core = self
        self.istft_calls = []
        self.resample_lengths = list(resample_lengths or [])
        self.resample_calls = 0

    def istft(self, stft_matrix, hop_length=None, length=None):
        self.istft_calls.append({"shape": stft_matrix.shape, "hop": hop_length, "length": length})
        count = int(length if length is not None else stft_matrix.shape[-1] * hop_length)
        return np.zeros(count, dtype=np.float32)

    def resample(self, audio, orig_sr, target_sr, *args, **kwargs):
        index = self.resample_calls % 3
        self.resample_calls += 1
        target = self.resample_lengths[index] if self.resample_lengths else int(audio.shape[-1])
        return np.zeros(audio.shape[:-1] + (target,), dtype=audio.dtype)


_FAKE_BANDS = {
    1: {"sr": 7350, "hl": 80, "n_fft": 640},
    2: {"sr": 7350, "hl": 80, "n_fft": 320},
    3: {"sr": 14700, "hl": 160, "n_fft": 512},
    4: {"sr": 44100, "hl": 480, "n_fft": 960},
}


def _fake_wave_to_spectrogram_mt(wave, hop_length, n_fft, *args, **kwargs):
    return np.zeros((2, int(n_fft) // 2 + 1, 1), dtype=np.complex64)


def _fake_spectrogram_to_wave(spec, hop_length, mid_side, mid_side_b2, reverse):
    left = librosa.istft(spec[0], hop_length=hop_length)
    right = librosa.istft(spec[1], hop_length=hop_length)
    if reverse:
        return np.stack((left[::-1], right[::-1]))
    if mid_side:
        return np.stack((left + right / 2, left - right / 2))
    if mid_side_b2:
        return np.stack((right / 1.25 + 0.4 * left, left / 1.25 - 0.4 * right))
    return np.stack((left, right))


def _fake_cmb_spectrogram_to_wave(spec_m, mp, extra_bins_h=None, extra_bins=None):
    wave = None
    for band_id in range(1, len(mp.param["band"]) + 1):
        band = mp.param["band"][band_id]
        spec = np.zeros((2, int(band["n_fft"]) // 2 + 1, 1), dtype=np.complex64)
        current = spectrogram_to_wave(
            spec, band["hl"], mp.param["mid_side"], mp.param["mid_side_b2"], mp.param["reverse"]
        )
        if band_id == len(mp.param["band"]):
            wave = current if wave is None else wave + current
        elif band_id == 1:
            next_band = mp.param["band"][band_id + 1]
            wave = librosa.resample(current, band["sr"], next_band["sr"])
        else:
            next_band = mp.param["band"][band_id + 1]
            wave = librosa.core.resample(wave + current, band["sr"], next_band["sr"])
    return wave.T


def _fake_spec_module(fake_librosa):
    return types.SimpleNamespace(
        librosa=fake_librosa,
        wave_to_spectrogram_mt=_fake_wave_to_spectrogram_mt,
        spectrogram_to_wave=_fake_spectrogram_to_wave,
        cmb_spectrogram_to_wave=_fake_cmb_spectrogram_to_wave,
    )


class UVRExactLengthTests(unittest.TestCase):
    def _proxy_with_captured_lengths(self, fake_librosa, counts):
        mp = types.SimpleNamespace(param={"band": _FAKE_BANDS, "mid_side": False,
                                         "mid_side_b2": False, "reverse": False})
        original = _fake_spec_module(fake_librosa)
        proxy, tracker = build_exact_length_spec_proxy(original, mp, np)
        for band_id in sorted(_FAKE_BANDS, reverse=True):
            band = _FAKE_BANDS[band_id]
            wave = np.zeros((2, counts[band_id]), dtype=np.float32)
            proxy.wave_to_spectrogram_mt(wave, band["hl"], band["n_fft"])
        return original, proxy, tracker, mp

    def test_real_band_sample_counts_drive_istft_for_4s_and_15s_endpoints(self):
        cases = (
            {1: 29400, 2: 29400, 3: 58800, 4: 176400},
            {1: 110250, 2: 110250, 3: 220500, 4: 661500},
        )
        for counts in cases:
            with self.subTest(high_band_samples=counts[4]):
                fake_librosa = _FakeLibrosa()
                _, proxy, tracker, mp = self._proxy_with_captured_lengths(fake_librosa, counts)
                tracker.require_complete_capture()
                for band_id, band in _FAKE_BANDS.items():
                    matrix = np.zeros((2, band["n_fft"] // 2 + 1, 1), dtype=np.complex64)
                    self.assertEqual(tracker.istft_length(matrix, band["hl"]), counts[band_id])
                self.assertEqual([row["source_samples"] for row in tracker.partial_evidence()["band_source_lengths"]],
                                 [counts[index] for index in (4, 3, 2, 1)])

    def test_cloned_spectrogram_keeps_all_five_branches_and_corrects_only_one_sample(self):
        counts = {1: 11, 2: 12, 3: 15, 4: 20}
        fake_librosa = _FakeLibrosa(resample_lengths=[11, 16, 20])
        original, proxy, tracker, mp = self._proxy_with_captured_lengths(fake_librosa, counts)
        for _ in range(2):
            proxy.cmb_spectrogram_to_wave(np.zeros((2, 10, 1), dtype=np.complex64), mp)
        evidence = tracker.finish()
        self.assertEqual(evidence["combination_count"], 2)
        self.assertEqual(len(evidence["istft_lengths"]), 16)
        self.assertEqual([row["expected_samples_from_measured_target_band"]
                          for row in evidence["resample_length_corrections"]],
                         [12, 15, 20, 12, 15, 20])
        self.assertEqual([row["correction_samples"] for row in evidence["resample_length_corrections"]],
                         [1, -1, 0, 1, -1, 0])
        self.assertIs(original.spectrogram_to_wave, _fake_spectrogram_to_wave)
        self.assertEqual(fake_librosa.istft_calls[0]["length"], 11)
        self.assertEqual(fake_librosa.istft_calls[1]["length"], 11)

    def test_resample_that_needs_more_than_one_sample_correction_is_rejected(self):
        counts = {1: 11, 2: 12, 3: 15, 4: 20}
        fake_librosa = _FakeLibrosa(resample_lengths=[10, 15, 20])
        _, proxy, _, mp = self._proxy_with_captured_lengths(fake_librosa, counts)
        with self.assertRaisesRegex(ExactLengthError, "超过 1 sample"):
            proxy.cmb_spectrogram_to_wave(np.zeros((2, 10, 1), dtype=np.complex64), mp)

    def test_unknown_band_signature_is_not_guessed(self):
        counts = {1: 11, 2: 12, 3: 15, 4: 20}
        fake_librosa = _FakeLibrosa()
        _, proxy, _, _ = self._proxy_with_captured_lengths(fake_librosa, counts)
        with self.assertRaisesRegex(ExactLengthError, "UVR"):
            proxy.wave_to_spectrogram_mt(np.zeros((2, 10)), 80, 1234)


if __name__ == "__main__":
    unittest.main()
