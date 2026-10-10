import json
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from core.rmvpe_adapter import load_rmvpe, pitch_frames, rmvpe_vc_class
from core.rvc_worker import capture_f0_class, floating_pipeline_class, write_f0_track, load_voice_checkpoint


class QuantizedVendorFixture:
    __module__ = 'vc_infer_pipeline'
    def pipeline(self, audio):
        audio_opt = audio.copy()
        audio_max = np.abs(audio_opt).max() / .99
        max_int16 = 32768
        if audio_max > 1:
            max_int16 /= audio_max
        audio_opt = (audio_opt * max_int16).astype(np.int16)
        return audio_opt


class UnknownVendorFixture:
    __module__ = 'vc_infer_pipeline'
    def pipeline(self, audio):
        return audio.astype(np.int16)


class RMVPEAdapterTests(unittest.TestCase):
    def test_untrusted_voice_checkpoint_cannot_execute_pickle_payload(self):
        import torch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, model = root / 'executed', root / 'voice.pth'
            class Payload:
                def __reduce__(self):
                    return eval, ("__import__('pathlib').Path(%r).write_text('executed')" % str(marker),)
            torch.save({'weight': {'bad': Payload()}, 'config': [1, 2, 48000]}, model)
            with self.assertRaises(Exception):
                load_voice_checkpoint(model, torch)
            self.assertFalse(marker.exists())
            from test_safe_checkpoint import CONFIG
            torch.save({'weight': {'emb_g.weight': torch.zeros(1, 256)}, 'config': CONFIG}, model)
            loaded = load_voice_checkpoint(model, torch)
            self.assertEqual(loaded['config'], CONFIG)
            self.assertEqual(tuple(loaded['weight']['emb_g.weight'].shape), (1, 256))

    def test_progress_counts_retained_samples_after_context_crop(self):
        class Base:
            t_pad_tgt = 100
            def vc(self, length):
                return np.zeros(length + 2 * self.t_pad_tgt)
            def pipeline(self, model, generator, sid, audio):
                return np.concatenate([self.vc(300), self.vc(700)])
        vc = capture_f0_class(Base)()
        vc._output_rate = 16000
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            vc.pipeline(None, None, 0, np.zeros(1000))
        rows = [json.loads(line)['worker_progress'] for line in captured.getvalue().splitlines()]
        self.assertEqual([row['fraction'] for row in rows], [0, .3, 1, 1])
        self.assertEqual(rows[2]['processed_samples'], 1000)
        self.assertEqual(rows[2]['completed_segments'], 2)
        self.assertEqual(rows[-1]['phase'], 'inference_complete')

    def test_float_adapter_preserves_gain_and_removes_only_quantization(self):
        samples = np.array([.123456789, -.2356789, 1.2], dtype=np.float32)
        old = QuantizedVendorFixture().pipeline(samples).astype(np.float32) / 32768
        adapted = floating_pipeline_class(QuantizedVendorFixture)().pipeline(samples)
        self.assertEqual(adapted.dtype, np.float32)
        self.assertLess(float(np.max(np.abs(adapted - old))), 1 / 32768)
        self.assertAlmostEqual(float(np.max(np.abs(adapted))), .99, places=6)
        self.assertTrue(np.any(np.abs(adapted * 32768 - np.round(adapted * 32768)) > .01))
        with self.assertRaises(RuntimeError):
            floating_pipeline_class(UnknownVendorFixture)

    def test_real_contour_is_shifted_and_coarse_is_bounded(self):
        extractor = mock.Mock()
        extractor.infer_from_audio.return_value = np.array([0., 100., 200., 2000., 0.])
        coarse, hz = pitch_frames(extractor, np.ones(640), 4, 12)
        np.testing.assert_array_equal(hz, [0., 200., 400., 4000.])
        self.assertEqual(coarse.dtype, np.int64)
        self.assertEqual(coarse[0], 1)
        self.assertEqual(coarse[-1], 255)
        self.assertEqual(extractor.infer_from_audio.call_args.kwargs, {'thred': .03})

    def test_missing_invalid_and_misaligned_contours_fail(self):
        with self.assertRaises(FileNotFoundError):
            load_rmvpe('missing-rmvpe.pt')
        for values in ([np.nan], [-1.], [], [100.] * 20):
            with self.subTest(values=values), self.assertRaises(ValueError):
                pitch_frames(mock.Mock(infer_from_audio=mock.Mock(return_value=values)), np.ones(320), 2, 0)

    def test_legacy_and_rmvpe_dispatch_are_distinct_and_lazy(self):
        class Base:
            x_pad = 1
            def get_f0(self, *args):
                return 'legacy', args
        instance = rmvpe_vc_class(Base, 'weight.pt', 'cpu')()
        extractor = mock.Mock(infer_from_audio=mock.Mock(return_value=[100., 0.]))
        with mock.patch('core.rmvpe_adapter.load_rmvpe', return_value=extractor) as loader:
            self.assertEqual(instance.get_f0('x', np.ones(320), 2, 0, 'pm', 3)[0], 'legacy')
            loader.assert_not_called()
            instance.get_f0('x', np.ones(320), 2, 0, 'rmvpe', 3)
            instance.get_f0('x', np.ones(320), 2, 0, 'rmvpe', 3)
            loader.assert_called_once_with('weight.pt', 'cpu')

    def test_capture_round_trip_does_not_fake_missing_f0(self):
        class Base:
            x_pad = 1
            def get_f0(self):
                return np.array([1, 2]), np.array([0., 100.])
        vc = capture_f0_class(Base)()
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'track.json')
            write_f0_track(path, vc, 'pm')
            self.assertEqual(json.loads(Path(path).read_text())['status'], 'UNOBSERVABLE')
            vc.get_f0()
            write_f0_track(path, vc, 'pm')
            record = json.loads(Path(path).read_text())
            self.assertEqual(record['actual_f0'], 'pm')
            self.assertEqual(record['frames_hz'], [0., 100.])


if __name__ == '__main__':
    unittest.main()
