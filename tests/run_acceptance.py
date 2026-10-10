"""Real installed engines acceptance; writes incremental evidence for long runs."""
import os
import sys
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import traceback
import time
from pathlib import Path
from core.pipeline import AudioPipeline
from core.storage import sha256_file
from core.processes import run,Control
from core.config import BASE_DIR,FFMPEG_EXE
ROOT=Path(BASE_DIR)
OUT=ROOT/'output'/'acceptance'
OUT.mkdir(exist_ok=True,parents=True)
evidence=OUT/'acceptance_results.json'
results=[]
pipeline=AudioPipeline()

def persist():
    with open(evidence,'w',encoding='utf-8') as f:json.dump(results,f,ensure_ascii=False,indent=2)
def case(name,path,**params):
    print('START_CASE',name,flush=True)
    before=sha256_file(path)
    t=time.monotonic()
    try:
        result=pipeline.process_file(str(path),custom_output_dir=str(OUT),control=Control(),**params)
        item=dict(name=name,status=result['qc']['status'],elapsed=time.monotonic()-t,
                  original_sha256=before,source_unchanged=before==sha256_file(path),manifest=result)
        assert item['source_unchanged']
        assert result['qc']['status']!='FAIL'
        item['stages']=pipeline.storage.list_stages(result['job_id'])
    except Exception as exc:
        item=dict(name=name,status='FAIL',elapsed=time.monotonic()-t,error=str(exc),traceback=traceback.format_exc(),
                  source_unchanged=before==sha256_file(path))
    results.append(item);persist()
    print('END_CASE',name,item['status'],round(item['elapsed'],2),flush=True)
    return item

short=ROOT/'temp'/'验收 中文短样本.wav'
case('15s_voice_zhou_with_midi',short,mode='voice_conversion',rvc_model_key='zhoujielun',
     clean_preset='light',generate_midi=True,output_formats=['wav','mp3','flac'])
case('15s_voice_zhaolusi',short,mode='voice_conversion',rvc_model_key='zhaolusi',
     clean_preset='light',output_formats=['wav','mp3'])
case('15s_four_stem_transcription',short,mode='transcribe',clean_preset='light',output_formats=['wav','mp3','flac'])
mono=ROOT/'temp'/'单声道 中文 10秒.m4a'
run([FFMPEG_EXE,'-y','-v','error','-i',str(short),'-t','10','-ac','1','-c:a','aac',str(mono)])
case('10s_mono_m4a_polish',mono,mode='polish',clean_preset='light',output_sample_rate=44100,output_formats=['wav','mp3','flac'])
case('full_song_sunyanzi_with_midi',ROOT/'song'/'春天刚到.mp3',mode='voice_conversion',rvc_model_key='sunyanzi',
     pitch_shift=7,clean_preset='light',generate_midi=True,output_formats=['wav','mp3','flac'])
pipeline.storage.close()
print('EVIDENCE',str(evidence),flush=True)
sys.exit(1 if any(x['status']=='FAIL' for x in results) else 0)

