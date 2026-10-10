"""Injected CUDA OOM followed by a real fresh-process CPU RVC inference."""
import sys,json,os
from pathlib import Path
from subprocess import CompletedProcess
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import soundfile as sf
import numpy as np
from core.config import RVC_PYTHON
from core.processes import Control
from core.worker_runtime import run_worker_with_recovery
OUT=ROOT/'output'/'acceptance'/'audit_repair'
cases=json.loads((OUT/'repair_results.json').read_text(encoding='utf-8'))
m=cases[0]['manifest'];info=m['job_spec']['effective_rvc']
source=Path(m['output_paths']['stems_dir'])/'02_vocal_cleaned.wav'
output=OUT/'cpu_fallback.wav'
cmd=[RVC_PYTHON,str(ROOT/'core'/'rvc_worker.py'),'--input',str(source),'--output',str(output),'--model',info['pth'],'--index',info.get('index') or '', '--pitch','0','--f0','harvest']
calls=[]
def runner(argv,**kwargs):
 device=argv[argv.index('--device')+1];calls.append(device)
 if device != 'cpu':
  error=dict(worker_error=dict(category='cuda_oom',phase='model_load' if len(calls)==1 else 'inference',device='cuda'))
  return CompletedProcess(argv,1,b'',json.dumps(error).encode())
 return Control().run(argv,**kwargs)
result=run_worker_with_recovery(cmd,[str(output)],timeout=180,gpu_overrides={'x-pad':1,'x-query':2,'x-center':12,'x-max':18},sidecar_path=str(output)+'.worker.json',run_fn=runner)
data,sr=sf.read(output,dtype='float32',always_2d=True)
assert np.isfinite(data).all() and len(data)>0
assert abs(len(data)/sr-sf.info(source).duration)<.1
assert calls==['auto','cuda','cpu']
evidence=dict(status='PASS',cuda_oom='injected, not real VRAM exhaustion',cpu_inference='real',devices=calls,sample_rate=sr,duration=len(data)/sr,finite=True,worker_metadata=result.worker_metadata)
(OUT/'cpu_fallback_results.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
print(json.dumps(evidence),flush=True)
