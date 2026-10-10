"""Real decode/DSP failures and artifact recovery, no mocked successful output."""
import os
import sys
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
import soundfile as sf
from core.pipeline import AudioPipeline
from core.storage import JobStore, sha256_file

root=Path(__file__).resolve().parent.parent
out=root/'output/acceptance/recovery'
out.mkdir(parents=True,exist_ok=True)
sr=48000
t=np.arange(sr*5)/sr
source=out/'故障恢复 音频.wav'
sf.write(str(source),(.2*np.sin(t*2*np.pi*330)*(1+.3*np.sin(t*7))).astype('float32'),sr,subtype='FLOAT')
pipeline=AudioPipeline(db_path=str(out/'recovery.db'))
original_hash=sha256_file(source)
checks={}
with patch.object(pipeline,'_clean',side_effect=RuntimeError('验收注入一次清理失败')):
    try:pipeline.process_file(str(source),mode='polish',clean_preset='heavy',output_formats=['wav','mp3'],custom_output_dir=str(out))
    except RuntimeError:pass
job=pipeline.storage.list_jobs(1)[0]
assert job['status']=='FAILED'
stages={x['stage_name']:x for x in pipeline.storage.list_stages(job['id'])}
assert stages['Ingest']['status']=='DONE' and stages['Clean']['status']=='FAILED'
checks['failure_persisted']=True
result=pipeline.process_file(str(source),resume_job_id=job['id'])
after={x['stage_name']:x for x in pipeline.storage.list_stages(job['id'])}
assert after['Ingest']['attempt']==stages['Ingest']['attempt']
assert after['Clean']['attempt']>stages['Clean']['attempt']
checks['resume_does_not_redecode']=True
wav=result['output_paths']['master_wav']
os.remove(wav)
result=pipeline.process_file(str(source),resume_job_id=job['id'])
assert os.path.isfile(wav)
checks['missing_master_rebuilt']=True
standard=after['Ingest']['outputs']['standard_wav']
with open(standard,'wb') as f:f.write(b'corrupted')
result=pipeline.process_file(str(source),resume_job_id=job['id'])
latest={x['stage_name']:x for x in pipeline.storage.list_stages(job['id'])}
assert latest['Ingest']['attempt']>after['Ingest']['attempt']
checks['corrupt_cache_redecoded']=True
assert sha256_file(source)==original_hash
checks['source_unchanged']=True
with open(source,'ab') as f:f.write(b'changed')
try:pipeline.process_file(str(source),resume_job_id=job['id'])
except RuntimeError as exc:
    assert 'hash' in str(exc)
    checks['source_change_blocks_resume']=True
else:raise AssertionError('Changed source silently resumed')
pipeline.storage.close()
with open(root/'output/acceptance/recovery_results.json','w',encoding='utf-8') as f:json.dump(checks,f,ensure_ascii=False,indent=2)
print(json.dumps(checks),flush=True)

