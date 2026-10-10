"""Real Tk queue/resume and selection acceptance with isolated settings/state."""
import sys,time,json,tempfile,importlib.machinery
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import core.config as config
from core.pipeline import AudioPipeline
OUT=ROOT/'output'/'acceptance'/'audit_repair'/'gui'
OUT.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='repair-gui-') as settings:
 config.CONFIG_DIR=settings
 ui=importlib.machinery.SourceFileLoader('repair_gui',str(ROOT/'AI_Music_Processor.pyw')).load_module()
 factory=lambda **kwargs:AudioPipeline(db_path=str(OUT/'gui.db'),**kwargs)
 with patch.object(ui,'AudioPipeline',side_effect=factory):app=ui.AIMusicProcessorApp()
 app.withdraw();app.update()
 app.mode.set('polish');app.pitch.set('0');app.output.set(str(OUT))
 app.add_files([str(ROOT/'temp'/'验收 中文短样本.wav'),str(ROOT/'temp'/'单声道 中文 10秒.m4a')])
 app.start();deadline=time.monotonic()+90
 while app.is_running and time.monotonic()<deadline:app.update();time.sleep(.03)
 assert not app.is_running,'queue timed out'
 rows=app.tree.get_children()
 assert len(rows)==2 and all('QC PASS' in app.tree.item(k)['values'][1] for k in rows)
 job_id=app.result_manifests[rows[0]]['job_id']
 app.refresh_history();app.history_tree.selection_set(job_id);app.update()
 assert app.selected_manifest()['job_id']==job_id,'history selection opened other queue result'
 before=[s['attempt'] for s in app.pipeline.storage.list_stages(job_id)]
 app.resume_history();deadline=time.monotonic()+90
 while app.is_running and time.monotonic()<deadline:app.update();time.sleep(.03)
 assert not app.is_running
 after=[s['attempt'] for s in app.pipeline.storage.list_stages(job_id)]
 assert before==after,'resume unexpectedly rebuilt valid stages'
 result=dict(status='PASS',real_tk_queue=2,history_selection=True,frozen_snapshot_gui_resume=True)
 (OUT/'gui_results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
 app.close()
 print(json.dumps(result),flush=True)
