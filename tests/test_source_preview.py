import tempfile,unittest
from pathlib import Path
import numpy as np
import soundfile as sf
from core.source_preview import preview_source


class SourcePreviewTests(unittest.TestCase):
    def test_real_readonly_preview_has_levels_and_music_estimates(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'待处理 源.wav'
            sf.write(source,.15*np.sin(2*np.pi*220*np.arange(48000*4)/48000),48000,subtype='PCM_24')
            before=source.read_bytes()
            report=preview_source(source,root/'work')
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual(report['bit_depth'],24);self.assertEqual(report['channels'],1)
            self.assertAlmostEqual(report['duration'],4)
            self.assertIsInstance(report['bpm_estimate'],float)
            self.assertTrue(report['key_estimate']);self.assertFalse(report['writes_source'])
            self.assertEqual(list((root/'work').iterdir()),[])
