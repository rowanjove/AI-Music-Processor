# -*- coding: utf-8 -*-
import sys,time,json,importlib.machinery,tempfile
from pathlib import Path
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root))
import core.config as config
isolated=Path(tempfile.mkdtemp(prefix='gui-acceptance-config-',dir=root/'temp'))
config.CONFIG_DIR=str(isolated)
ui=importlib.machinery.SourceFileLoader('music_ui_acceptance',str(root/'AI_Music_Processor.pyw')).load_module()
app=ui.AIMusicProcessorApp();app.withdraw()
app.mode.set('polish');app.pitch.set('0');app.output.set(str(root/'output/acceptance/gui_queue'))
paths=[str(root/'temp/验收 中文短样本.wav'),str(root/'temp/单声道 中文 10秒.m4a')]
app.add_files(paths)
dispatch_ids={item['dispatch_id'] for item in app.pending.values()}
app.start()
# UI edits during processing must not change the already dispatched parameters.
app.mode.set('voice_conversion');app.pitch.set('12')
deadline=time.monotonic()+180
while app.is_running and time.monotonic()<deadline:
    app.update();time.sleep(.05)
assert not app.is_running,'Queue timed out'
states=[app.tree.item(key)['values'][1] for key in app.tree.get_children()]
assert len(states)==2 and all('QC PASS' in x for x in states),states
jobs=[job for job in app.pipeline.storage.list_jobs(20) if job['parameters'].get('dispatch_id') in dispatch_ids]
assert len(jobs)==2 and all(job['mode']=='polish' and job['parameters']['pitch_shift']==0 for job in jobs)
result=dict(status='PASS',states=states,parameter_snapshot=True,dispatch_ids=[j['parameters']['dispatch_id'] for j in jobs[:2]])
(root/'output/acceptance/gui_queue_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
app.close()
print(json.dumps(result,ensure_ascii=False))
