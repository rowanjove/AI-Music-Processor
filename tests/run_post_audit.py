# -*- coding: utf-8 -*-
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import numpy as np
import soundfile as sf
from core.aligner import align_vocal_timing
from core.qc import run_quality_control
root=Path(__file__).resolve().parent.parent
cases=json.loads((root/'output/acceptance/acceptance_results.json').read_text(encoding='utf-8'))
m=cases[-1]['manifest'];stems=Path(m['output_paths']['stems_dir'])
out=root/'temp/final_post_audit_aligned.wav'
align_vocal_timing(stems/'01_vocal_original.wav',stems/'03_vocal_rvc_sunyanzi.wav',out)
a,sr=sf.read(out,dtype='float32',always_2d=True)
b,other_sr=sf.read(stems/'03_vocal_aligned.wav',dtype='float32',always_2d=True)
assert sr==other_sr and a.shape==b.shape
error=float(np.max(np.abs(a-b)))
assert error<1e-7,error
qc,_=run_quality_control(m['output_paths']['master_wav'],m['output_paths']['master_mp3'],'春天刚到.mp3','voice_conversion',alignment_path=str(out)+'.json',stem_paths=list(map(str,stems.glob('*.wav'))),extra_exports=[m['output_paths']['master_flac']],expected_duration=289.795917)
assert qc['status']=='PASS',qc
result=dict(status='PASS',alignment_max_sample_difference=error,qc=qc)
(root/'output/acceptance/post_audit_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print('PASS',error,qc['alignment']['max_local_residual_ms'])
