"""Post-audit real-engine acceptance in a separate database/output directory."""
import sys, json, time, traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core.pipeline import AudioPipeline
from core.processes import Control
from core.storage import sha256_file
OUT=ROOT/'output'/'acceptance'/'audit_repair'
OUT.mkdir(parents=True,exist_ok=True)
SOURCE=ROOT/'temp'/'验收 中文短样本.wav'
pipeline=AudioPipeline(db_path=str(OUT/'repair.db'))
results=[]
cases=[('voice_midi_all_exports',dict(mode='voice_conversion',rvc_model_key='zhoujielun',generate_midi=True,output_formats=['wav','mp3','flac'])),
       ('four_stem_midi',dict(mode='transcribe',output_formats=['wav','mp3','flac'])),
       ('mono_m4a_polish',dict(mode='polish',output_sample_rate=44100,output_formats=['wav','mp3','flac']))]
try:
 for name,params in cases:
  source=ROOT/'temp'/'单声道 中文 10秒.m4a' if name=='mono_m4a_polish' else SOURCE
  start=time.monotonic(); before=sha256_file(source)
  print('START',name,flush=True)
  try:
   manifest=pipeline.process_file(str(source),custom_output_dir=str(OUT),control=Control(),**params)
   assert manifest['qc']['status']!='FAIL'
   assert sha256_file(source)==before
   stages=pipeline.storage.list_stages(manifest['job_id'])
   assert all(stage['status']=='DONE' for stage in stages)
   if name=='voice_midi_all_exports':
    assert manifest['worker_execution']
    resumed=pipeline.process_file(str(source),resume_job_id=manifest['job_id'])
    assert resumed['qc']['status']!='FAIL'
    assert [s['attempt'] for s in stages]==[s['attempt'] for s in pipeline.storage.list_stages(manifest['job_id'])]
   result=dict(name=name,status=manifest['qc']['status'],elapsed=round(time.monotonic()-start,2),source_unchanged=True,manifest=manifest)
  except Exception as exc:
   result=dict(name=name,status='FAIL',error=str(exc),traceback=traceback.format_exc())
  results.append(result)
  (OUT/'repair_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
  print('END',name,result['status'],flush=True)
finally:pipeline.storage.close()
sys.exit(1 if any(r['status']=='FAIL' for r in results) else 0)
