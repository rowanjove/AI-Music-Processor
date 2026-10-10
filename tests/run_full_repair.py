"""Final full-song real-model acceptance after all source repairs."""
import sys,json,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core.pipeline import AudioPipeline
from core.processes import Control
from core.storage import sha256_file
OUT=ROOT/'output/acceptance/audit_repair';source=ROOT/'song/春天刚到.mp3'
pipeline=AudioPipeline(db_path=str(OUT/'repair.db'))
before=sha256_file(source);started=time.monotonic()
try:
 manifest=pipeline.process_file(str(source),mode='voice_conversion',rvc_model_key='sunyanzi',pitch_shift=7,generate_midi=True,output_formats=['wav','mp3','flac'],custom_output_dir=str(OUT),control=Control())
 assert sha256_file(source)==before
 assert manifest['qc']['status']!='FAIL'
 assert manifest['worker_execution'] and manifest['dependencies']
 result=dict(status=manifest['qc']['status'],elapsed_seconds=round(time.monotonic()-started,2),source_unchanged=True,source_sha256=before,manifest=manifest)
except Exception as exc:
 result=dict(status='FAIL',error=str(exc),traceback=traceback.format_exc(),elapsed_seconds=round(time.monotonic()-started,2))
finally:pipeline.storage.close()
(OUT/'full_song_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(status=result['status'],elapsed=result['elapsed_seconds'])),flush=True)
sys.exit(1 if result['status']=='FAIL' else 0)
