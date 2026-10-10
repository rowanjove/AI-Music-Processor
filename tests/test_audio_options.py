import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.analyzer import analyze_audio,standardize_to_48k_float
from core.mastering import master_bus
from core.job_spec import validate,validate_audio_options


class AudioOptionsTests(unittest.TestCase):
    def test_mono_input_and_export_are_real_and_maintain_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.wav';converted=root/'mono-stereo.wav';master=root/'master.wav'
            rate=48000;t=np.arange(rate*4)/rate
            wave=np.column_stack((.15*np.sin(2*np.pi*220*t),.08*np.sin(2*np.pi*660*t))).astype('float32')
            sf.write(source,wave,rate,subtype='FLOAT');original=source.read_bytes()
            standardize_to_48k_float(source,converted,input_channel_mode='mono')
            stereo,sr=sf.read(converted,always_2d=True)
            self.assertEqual(sr,48000);self.assertEqual(stereo.shape,wave.shape)
            np.testing.assert_array_equal(stereo[:,0],stereo[:,1])
            self.assertGreater(np.max(np.abs(wave[:,0]-wave[:,1])),.01)
            master_bus(converted,master,target_lufs=-16,output_channels=1)
            info=sf.info(master);self.assertEqual(info.channels,1)
            self.assertAlmostEqual(info.duration,4,places=3)
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(list(root.glob('.master-mono-*')),[])
            self.assertEqual(json.loads(Path(str(master)+'.json').read_text())['output_channels'],1)

    def test_analysis_off_skips_music_estimates_but_keeps_safety(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'input.wav';t=np.arange(48000*2)/48000
            sf.write(source,.1*np.sin(2*np.pi*220*t),48000,subtype='FLOAT')
            with patch('librosa.beat.tempo',side_effect=AssertionError('musical estimate executed')):
                report=analyze_audio(source,detailed_analysis=False)
            self.assertIsNone(report['bpm_estimate']);self.assertIsNone(report['key_estimate'])
            self.assertEqual(report['musical_analysis_seconds'],0)
            self.assertIn('integrated_lufs',report);self.assertIn('repair',report)

    def test_old_frozen_spec_not_mutated_and_malformed_options_rejected(self):
        fixture=json.loads((ROOT/'output/acceptance/audit_repair/验收 中文短样本_20261004_192142_144337/project.json').read_text(encoding='utf-8'))
        old=copy.deepcopy(fixture['job_spec']);original=copy.deepcopy(old)
        self.assertEqual(validate(old,'voice_conversion'),original)
        for changes in ({'output_channels':True},{'output_channels':1.0},{'output_channels':3},
                        {'input_channel_mode':[]},{'detailed_analysis':1}):
            with self.assertRaises(ValueError):validate_audio_options(changes)


if __name__=='__main__':unittest.main()
