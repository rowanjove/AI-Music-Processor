import json
import os
import tempfile
import unittest
import numpy as np
import soundfile as sf
from core.bounded_voice_dsp import process, validate_profile
from core.execution_policy import freeze_execution_policy, validate_execution_policy


class BoundedVoiceDspTests(unittest.TestCase):
    def test_cancel_cleans_partial_and_preserves_source(self):
        from unittest.mock import patch
        from core.processes import ProcessCancelled
        with tempfile.TemporaryDirectory() as directory:
            source=os.path.join(directory,'source.wav');dest=os.path.join(directory,'out.wav')
            sf.write(source,np.ones(48000)*.1,48000,subtype='FLOAT')
            original=open(source,'rb').read()
            with patch('core.processes.checkpoint',side_effect=ProcessCancelled('cancel')):
                with self.assertRaises(ProcessCancelled):process(source,dest)
            self.assertFalse(os.path.exists(dest))
            self.assertEqual(os.listdir(directory),['source.wav'])
            self.assertEqual(open(source,'rb').read(),original)

    def test_actual_adaptive_mix_bus_and_silence(self):
        from core.mixer import mix_stems
        with tempfile.TemporaryDirectory() as directory:
            t=np.arange(48000)/48000
            vocal=os.path.join(directory,'vocal.wav');inst=os.path.join(directory,'inst.wav')
            sf.write(vocal,.01*np.sin(2*np.pi*220*t),48000,subtype='FLOAT')
            sf.write(inst,np.repeat((.3*np.sin(2*np.pi*440*t))[:,None],2,axis=1),48000,subtype='FLOAT')
            output=os.path.join(directory,'mix.wav')
            result=mix_stems(vocal,inst,output,adaptive_mix=True,mix_bus_profile='gentle')
            data,rate=sf.read(result,always_2d=True)
            self.assertEqual((len(data),rate,data.shape[1]),(48000,48000,2))
            self.assertTrue(np.isfinite(data).all())
            with open(output+'.mix.json') as handle:receipt=json.load(handle)
            self.assertEqual(receipt['adaptive_correction_db'],2.)
            self.assertEqual(receipt['adaptive_compressor']['mix'], .2)
            self.assertLessEqual(receipt['adaptive_compressor']['dry_wet_maximum_attenuation_db'],2)
            self.assertTrue(os.path.exists(result+'.dsp.json'))
            sf.write(vocal,np.zeros(48000),48000,subtype='FLOAT')
            result=mix_stems(vocal,inst,output,adaptive_mix=True)
            with open(output+'.mix.json') as handle:receipt=json.load(handle)
            self.assertIsNone(receipt['vocal_lufs'])
            self.assertEqual(receipt['adaptive_correction_db'],0.)
            # Loud vocal with silent backing measures a different actual RMS
            # threshold. Comparing to explicit compressor bypass proves the
            # FFmpeg curve affects samples, beyond merely recording a setting.
            sf.write(vocal,.7*np.sin(2*np.pi*220*t),48000,subtype='FLOAT')
            sf.write(inst,np.zeros((48000,2)),48000,subtype='FLOAT')
            compressed=mix_stems(vocal,inst,os.path.join(directory,'compressed.wav'),adaptive_mix=True)
            bypass=mix_stems(vocal,inst,os.path.join(directory,'bypass.wav'),adaptive_mix=True,skip_mix_compressor=True)
            wet,_=sf.read(compressed,always_2d=True);dry,_=sf.read(bypass,always_2d=True)
            wet_rms=float(np.sqrt(np.mean(wet*wet)));dry_rms=float(np.sqrt(np.mean(dry*dry)))
            self.assertLess(wet_rms,dry_rms)
            self.assertGreaterEqual(wet_rms,.8*dry_rms-1e-6)
            with open(compressed+'.mix.json') as handle: loud=json.load(handle)
            self.assertGreater(loud['adaptive_compressor']['threshold'],receipt['adaptive_compressor']['threshold'])

    def test_streaming_shape_finite_and_measured_reduction(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'source.wav')
            t = np.arange(110003) / 48000
            audio = (.7*np.sin(2*np.pi*220*t)+.2*np.sin(2*np.pi*7000*t))[:, None]
            audio = np.repeat(audio, 2, axis=1)
            sf.write(source, audio, 48000, subtype='FLOAT')
            original = open(source, 'rb').read()
            for kind in ('post', 'bus'):
                dest = os.path.join(directory, kind+'.wav')
                self.assertEqual(process(source, dest, kind=kind), dest)
                actual, rate = sf.read(dest, always_2d=True)
                self.assertEqual(actual.shape, audio.shape)
                self.assertEqual(rate, 48000)
                self.assertTrue(np.isfinite(actual).all())
                self.assertLessEqual(float(np.max(np.abs(actual))), .98)
                with open(dest+'.dsp.json', encoding='utf-8') as handle:
                    receipt = json.load(handle)
                self.assertLessEqual(receipt['maximum_compressor_gain_reduction_db'], 2+1e-10)
                self.assertGreater(receipt['maximum_compressor_gain_reduction_db'], 0)
            self.assertEqual(open(source, 'rb').read(), original)
            self.assertEqual(process(source, os.path.join(directory, 'off.wav'), profile='off'), source)
            self.assertFalse(os.path.exists(os.path.join(directory, 'off.wav')))

    def test_reject_nonfinite_and_unsafe_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'nan.wav')
            sf.write(source, np.array([0., np.nan]), 48000, subtype='FLOAT')
            with self.assertRaises(ValueError):
                process(source, os.path.join(directory, 'out.wav'))
            self.assertFalse(os.path.exists(os.path.join(directory, 'out.wav')))
        for value in (True, None, 'fake'):
            with self.assertRaises(ValueError):
                validate_profile(value)

    def test_frozen_profiles_are_distinct_and_tamper_rejected(self):
        policies = [freeze_execution_policy('cpu', name) for name in ('compatibility','balanced','quality')]
        self.assertEqual([p['cpu_threads'] for p in policies], [1,2,4])
        self.assertEqual([p['rvc']['x-max'] for p in policies], [18,30,40])
        for policy in policies:
            self.assertEqual(validate_execution_policy(policy), policy)
        policies[0]['cpu_threads'] = 4
        with self.assertRaises(ValueError):
            validate_execution_policy(policies[0])
