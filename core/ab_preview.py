"""Same-start A/B previews of a completed project. Never writes source audio."""
import json
import math
import os
import uuid
from pathlib import Path
from core.config import FFMPEG_EXE
from core.processes import run
from core.storage import sha256_file
from core.pipeline_support import _output_paths


def project_audio_roles(project):
    if not isinstance(project,dict) or project.get('status')!='DONE':
        raise ValueError('A/B 试听要求已完成的项目')
    roles={}
    def add(role,path):
        if isinstance(path,str) and Path(path).is_file():roles[role]=str(Path(path).absolute())
    add('原始输入',project.get('source_path'))
    add('最终母带',(project.get('output_paths') or {}).get('master_wav'))
    for stage in project.get('stage_records',[]):
        if stage.get('status')!='DONE':continue
        for name,path in _output_paths(stage.get('outputs')).items():
            if Path(path).suffix.lower() in ('.wav','.flac','.mp3','.m4a','.ogg'):
                add(stage.get('stage_name','Stage')+'.'+name,path)
    if len(roles)<2:raise ValueError('此项目没有两个可比较的音频角色')
    return roles


def prepare_ab_preview(source,directory,start_seconds=0.,duration_seconds=30.,target_rms=None):
    start,duration=float(start_seconds),float(duration_seconds)
    if not math.isfinite(start) or start<0 or not math.isfinite(duration) or not 0<duration<=30:
        raise ValueError('试听起点必须非负，片段长度须为 0–30 秒')
    source=Path(source).absolute()
    if not source.is_file():raise FileNotFoundError(source)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    target=directory/('ab-'+uuid.uuid4().hex+'.wav')
    before=sha256_file(source)
    try:
        from core.analyzer import probe_metadata
        source_channels=int(probe_metadata(str(source)).get('channels') or 0)
        mapping=['-af','pan=stereo|c0=c0|c1=c0'] if source_channels==1 else []
        run([FFMPEG_EXE,'-nostdin','-hide_banner','-loglevel','error','-n',
             '-ss',str(start),'-i',str(source),'-t',str(duration),'-vn',
             *mapping,'-ar','48000','-ac','2','-c:a','pcm_s16le',str(target)],timeout=120)
        import soundfile as sf
        info=sf.info(target)
        if info.frames<=0 or info.duration>duration+.025 or info.samplerate!=48000:
            raise ValueError('试听片段为空或长度/采样率异常，请选择音频范围内的起点')
        if sha256_file(source)!=before:raise ValueError('试听准备期间源音频发生变化')

        import numpy as np
        data,sr=sf.read(target,always_2d=True)
        current_rms=float(np.sqrt(np.mean(data**2)))
        loudness_matched=False
        if target_rms is not None and math.isfinite(float(target_rms)) and float(target_rms)>0:
            if current_rms>1e-7:
                scale=float(target_rms)/current_rms
                max_abs=float(np.max(np.abs(data)))
                if max_abs*scale>0.98:
                    scale=0.98/max(max_abs,1e-7)
                matched_data=data*scale
                sf.write(target,matched_data,48000,subtype='PCM_16')
                current_rms=float(np.sqrt(np.mean(matched_data**2)))
                loudness_matched=True

        record={'source_path':str(source),'source_sha256':before,'preview_path':str(target),
            'preview_sha256':sha256_file(target),'start_seconds':start,'duration_seconds':info.duration,
            'sample_rate':48000,'channels':2,'source_modified':False,'rms':current_rms,
            'loudness_matched':loudness_matched,
            'channel_mapping':'duplicate_mono' if source_channels==1 else ('stereo' if source_channels==2 else 'stereo_downmix'),
            'quality_acceptance':'READY_FOR_EXTERNAL_VALIDATION','listening_quality':'not_assessed'}
        Path(str(target)+'.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return record
    except Exception:
        target.unlink(missing_ok=True)
        raise


def prepare_ab_pair(source_a,source_b,directory,start_seconds=0.,duration_seconds=30.,match_loudness=True):
    """Prepare a loudness-matched same-start A/B pair to avoid 'louder is better' bias."""
    rec_a=prepare_ab_preview(source_a,directory,start_seconds,duration_seconds)
    target_rms=rec_a.get('rms') if match_loudness else None
    rec_b=prepare_ab_preview(source_b,directory,start_seconds,duration_seconds,target_rms=target_rms)
    return rec_a,rec_b



def play_ab_preview(record):
    if os.name!='nt':raise RuntimeError('内置片段试听目前要求 Windows')
    path=record.get('preview_path')
    if not path or sha256_file(path)!=record.get('preview_sha256'):raise ValueError('试听片段已变化，请重新准备')
    import winsound
    # ASYNC replaces the previous A/B clip; every choice starts at this exact
    # prepared point rather than letting independent external players overlap.
    winsound.PlaySound(path,winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT)


def stop_ab_preview():
    if os.name=='nt':
        import winsound
        winsound.PlaySound(None,0)
