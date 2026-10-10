import os
import sys
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
import tempfile
import threading
import time
import numpy as np
import soundfile as sf
from scipy import signal
from core.aligner import align_vocal_timing
from core.analyzer import probe_metadata, measure_loudness_ebur128
from core.mastering import master_bus
from core.qc import run_quality_control
from core.processes import Control, ProcessCancelled

class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='中文 音频验收 ')
        self.root=self.tmp.name
        self.sr=48000
        t=np.arange(48000*3)/48000
        rng=np.random.default_rng(3)
        self.audio=((.3+.2*np.sin(t*7)**8)*np.sin(2*np.pi*220*t)+.04*rng.normal(size=len(t))).astype('float32')
        self.source=os.path.join(self.root,'参考.wav')
        sf.write(self.source,self.audio,self.sr,subtype='FLOAT')
    def tearDown(self):
        self.tmp.cleanup()
    def test_both_alignment_directions(self):
        for ms in (-85,85):
            n=round(abs(ms)/1000*self.sr)
            converted=np.concatenate((np.zeros(n),self.audio))[:len(self.audio)] if ms>0 else np.pad(self.audio[n:],(0,n))
            path=os.path.join(self.root,'延迟.wav')
            out=os.path.join(self.root,'对齐.wav')
            sf.write(path,converted,self.sr,subtype='FLOAT')
            _,correction=align_vocal_timing(self.source,path,out)
            data,_=sf.read(out)
            # Exclude boundaries which intentionally contain padding.
            # Voice conversion changes phase/timbre; timing accuracy is the contract.
            self.assertLess(abs(correction+ms),1.)
            import json
            with open(out+'.json') as f:metrics=json.load(f)
            self.assertLess(abs(metrics['residual_ms']),2)
    def test_damaged_file_does_not_get_defaults(self):
        path=os.path.join(self.root,'损坏.wav')
        with open(path,'wb') as f:f.write(b'garbage')
        with self.assertRaises(Exception):probe_metadata(path)
        with self.assertRaises(Exception):measure_loudness_ebur128(path)
    def test_local_drift_correction(self):
        sr=8000
        t=np.arange(sr*45)/sr
        rng=np.random.default_rng(14)
        envelope=signal.savgol_filter(rng.random(len(t)), 401, 2)
        audio=(envelope*np.sin(2*np.pi*233*t)).astype('float32')
        positions=np.arange(len(t))
        delays=np.interp(t,[0,15,30,45],[0,0,.032,.032])*sr
        converted=np.interp(positions-delays,positions,audio,left=0,right=0)
        ref=os.path.join(self.root,'drift_ref.wav');src=os.path.join(self.root,'drift.wav');out=os.path.join(self.root,'fixed.wav')
        sf.write(ref,audio,sr,subtype='FLOAT');sf.write(src,converted,sr,subtype='FLOAT')
        align_vocal_timing(ref,src,out)
        import json
        with open(out+'.json') as handle:metrics=json.load(handle)
        self.assertTrue(metrics['local_timewarp'])
        self.assertLess(metrics['max_local_residual_ms'],10)
    def test_silent_audio_rejected(self):
        path=os.path.join(self.root,'静音.wav')
        sf.write(path,np.zeros(self.sr),self.sr)
        with self.assertRaises(ValueError):measure_loudness_ebur128(path)
    def test_missing_export_rejected(self):
        with self.assertRaises(Exception):
            run_quality_control(self.source,os.path.join(self.root,'不存在.mp3'),'x','polish')
    def test_silent_vocal_cannot_hide_behind_instrumental(self):
        path=os.path.join(self.root,'03_vocal_aligned.wav')
        sf.write(path,np.zeros(self.sr*3),self.sr)
        data,_=run_quality_control(self.source,None,'x','voice_conversion',stem_paths=[path])
        self.assertEqual(data['status'],'FAIL')
        self.assertTrue(any('静音' in issue for issue in data['failures']))
    def test_mp3_flac_master_and_duration(self):
        wav=os.path.join(self.root,'母带.wav')
        mp3=os.path.join(self.root,'母带.mp3')
        flac=os.path.join(self.root,'母带.flac')
        master_bus(self.source,wav,mp3,target_lufs=-13,sample_rate=44100,output_flac=flac)
        result,_=run_quality_control(wav,mp3,'fixture','polish',expected_duration=3,target_lufs=-13,extra_exports=[flac])
        self.assertNotEqual(result['status'],'FAIL')
        self.assertEqual(probe_metadata(wav)['bit_depth'],24)
        self.assertEqual(probe_metadata(wav)['sample_rate'],44100)
        self.assertLessEqual(result['true_peak_dbtp'],-1)
    def test_cancel_running_process(self):
        control=Control()
        threading.Timer(.5,control.cancel).start()
        start=time.monotonic()
        with self.assertRaises(ProcessCancelled):
            control.run([sys.executable,'-c','import time;time.sleep(30)'])
        self.assertLess(time.monotonic()-start,5)

if __name__=='__main__':unittest.main()

