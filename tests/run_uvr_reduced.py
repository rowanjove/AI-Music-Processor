"""Real HP2 CUDA inference with a valid smaller window, plus boundary checks."""
import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import soundfile as sf
import numpy as np
from core.uvr_worker import run_uvr_separation, validate_window_size
from types import SimpleNamespace
OUT = ROOT/'output/acceptance/audit_repair'
for invalid in (128, 256, 300):
    try:
        validate_window_size(invalid, SimpleNamespace(offset=128))
    except ValueError:
        pass
    else:
        raise AssertionError('unsafe window accepted: '+str(invalid))
validate_window_size(384, SimpleNamespace(offset=128))
source = ROOT/'temp/验收 中文短样本.wav'
vocal, inst = OUT/'uvr_reduced_vocal.wav', OUT/'uvr_reduced_inst.wav'
run_uvr_separation(str(source), str(vocal), str(inst), device='cuda', window_size=384)
for output in (vocal, inst):
    data, sr = sf.read(output, dtype='float32', always_2d=True)
    assert len(data)>0 and np.isfinite(data).all()
    assert abs(len(data)/sr-sf.info(source).duration)<.1
evidence = dict(status='PASS', model='real HP2', device='cuda', window_size=384,
                rejected_windows=[128,256,300], outputs=[str(vocal),str(inst)])
(OUT/'uvr_reduced_results.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
print(json.dumps(evidence),flush=True)
