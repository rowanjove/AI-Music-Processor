import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.f0_method import resolve_f0_method
from core.rvc_backend import LEGACY, MODERN, backend_catalog, convert_modern
from core.ux_flow import help_text


class RvcBackendTests(unittest.TestCase):
    def test_modern_rmvpe_does_not_execute_harvest(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch("core.f0_method.resolve_f0_method", side_effect=AssertionError("must not run")):
            refused = convert_modern("rmvpe", root=folder)
            legacy = resolve_f0_method("rmvpe", root=folder)
        self.assertEqual(refused["requested_f0"], "rmvpe")
        self.assertIsNone(refused["actual_f0"])
        self.assertFalse(refused["executed"])
        self.assertEqual(refused["inference_status"], "NOT_RUN")
        self.assertIn("不会改跑 harvest", refused["fallback_reason"])
        self.assertIsNone(refused["output"])
        self.assertEqual(refused["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(legacy["executed"], "harvest")
        self.assertTrue(legacy["fallback"])

    def test_invalid_weight_cannot_advertise_a_runnable_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "rmvpe.pt"), "wb") as handle:
                handle.write(b"not-a-model")
            catalog = backend_catalog(folder)
            refused = convert_modern("rmvpe", root=folder)
        self.assertEqual(catalog["selectable"], [LEGACY])
        self.assertEqual(catalog["default_backend"], LEGACY)
        self.assertFalse(catalog["backends"][MODERN]["runnable"])
        self.assertTrue(catalog["backends"][MODERN]["implemented"])
        self.assertEqual(catalog["backends"][MODERN]["inference_status"], "NOT_RUN")
        self.assertTrue(catalog["backends"][MODERN]["weight_present"])
        self.assertFalse(catalog["backends"][LEGACY]["rmvpe"])
        self.assertTrue(refused["weight_present"])
        self.assertIsNone(refused["actual_f0"])
        self.assertEqual(refused["inference_status"], "NOT_RUN")

    def test_real_conversion_is_delegated_and_missing_weight_never_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                convert_modern('rmvpe', root=folder, input_vocal_wav='in.wav', output_rvc_wav='out.wav')
        with mock.patch('core.rvc_engine.convert_voice', return_value='out.wav') as convert:
            record = convert_modern('pm', input_vocal_wav='in.wav', output_rvc_wav='out.wav')
        self.assertTrue(record['executed'])
        self.assertEqual(record['actual_f0'], 'pm')
        convert.assert_called_once_with(f0_method='pm', input_vocal_wav='in.wav', output_rvc_wav='out.wav')

    def test_unknown_f0_is_rejected(self):
        with self.assertRaises(ValueError):
            convert_modern("dio")
