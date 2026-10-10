import sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from core.pipeline import AudioPipeline
from core.config import FFMPEG_EXE
from core.processes import run
from core.storage import sha256_file
root=Path(__file__).resolve().parent.parent
out=root/'output/acceptance'
temp=root/'temp'
p=AudioPipeline()
results=[]
for name,ext,codec,length in [('30s_long_chinese_wav','wav',['-c:a','pcm_f32le'],30),('10s_ogg','ogg',['-c:a','libvorbis'],10),('10s_opus','opus',['-c:a','libopus'],10),('10s_flac','flac',['-c:a','flac'],10)]:
    path=temp/((('中文长文件名验收_'*8) if length==30 else name)+'.'+ext)
    run([FFMPEG_EXE,'-y','-v','error','-ss','30','-i',str(root/'song/春天刚到.mp3'),'-t',str(length)]+codec+[str(path)])
    before=sha256_file(path)
    start=time.monotonic()
    result=p.process_file(str(path),mode='polish',output_formats=['wav','mp3','flac'],custom_output_dir=str(out))
    item=dict(name=name,status=result['qc']['status'],elapsed=time.monotonic()-start,source_unchanged=before==sha256_file(path),manifest=result)
    assert item['status']!='FAIL' and item['source_unchanged']
    results.append(item)
    (out/'boundary_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(name,item['status'],flush=True)
p.storage.close()
