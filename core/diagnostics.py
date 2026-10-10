"""Read-only checks of configured engines; never equate existence with readiness."""
import os
import json
import shutil
from core.config import (FFMPEG_EXE,FFPROBE_EXE,RVC_ROOT,RVC_PYTHON,UVR_ROOT,ADDONS_DIR,
                         AUDACITY_EXE,MUSESCORE_EXE,LMMS_EXE,NEURALNOTE_EXE,BASE_DIR,RELEASE_MODE,
                         get_available_rvc_models)
from core.processes import run

def diagnostics(runtime_result=None):
    result={}
    for name,path in [('FFmpeg',FFMPEG_EXE),('ffprobe',FFPROBE_EXE)]:
        try:
            out=run([path,'-version'],timeout=15).stdout.decode('utf-8',errors='replace')
            result[name]=dict(status='PASS',path=path,version=out.splitlines()[0])
        except Exception as exc:
            result[name]=dict(status='FAIL',path=path,error=str(exc))
    script="import sys,json,torch;sys.path.insert(0,"+repr(ADDONS_DIR)+");from demucs.pretrained import get_model;from basic_pitch.inference import predict;print(json.dumps(dict(cuda=torch.cuda.is_available(),gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU',torch=torch.__version__,python=sys.version)))"
    try:
        probe=run([RVC_PYTHON,'-c',script],timeout=60)
        result['Python/AI']=dict(status='PASS',path=RVC_PYTHON,details=json.loads(probe.stdout.decode('utf-8').splitlines()[-1]))
    except Exception as exc:
        result['Python/AI']=dict(status='FAIL',error=str(exc))
    for name,path in [('UVR two-stem',os.path.join(RVC_ROOT,'uvr5_weights','HP2_all_vocals.pth')),
                      ('Demucs four-stem',os.path.join(UVR_ROOT,'models','Demucs_Models','v3_v4_repo','955717e8-8726e21a.th')),
                      ('HuBERT',os.path.join(RVC_ROOT,'hubert_base.pt')),
                      ('Basic Pitch',os.path.join(ADDONS_DIR,'basic_pitch','saved_models','icassp_2022','nmp.onnx'))]:
        result[name]=dict(status='PASS' if os.path.isfile(path) and os.path.getsize(path)>10000 else 'FAIL',path=path)
    for name,path in [('Audacity',AUDACITY_EXE),('MuseScore',MUSESCORE_EXE),('LMMS',LMMS_EXE),('NeuralNote',NEURALNOTE_EXE)]:
        valid=False
        if os.path.isfile(path):
            with open(path,'rb') as f:valid=f.read(2)==b'MZ'
        result[name]=dict(status='WARN' if valid else 'FAIL',path=path,check='仅验证 Windows 可执行文件；编辑器实际导入及音频设备需单独验收')
    result['RVC models']={key:dict(path=info['pth'],index=info['index'],profile=info['profile']) for key,info in get_available_rvc_models().items()}
    if RELEASE_MODE:
        if runtime_result is not None:
            result['Runtime manifest']=runtime_result
        else:
            try:
                from core.runtime_verify import verify_runtime
                runtime_result=verify_runtime(root=BASE_DIR,timeout=900)
                result['Runtime manifest']=runtime_result
            except Exception as exc:
                result['Runtime manifest']=dict(status='FAIL',error=str(exc))
    else:
        result['Runtime manifest']=dict(status='N/A',message='开发模式不运行 Portable 启动门禁')
    result['Disk']=dict(free_gb=round(shutil.disk_usage(BASE_DIR).free/1024**3,2))
    from core.component_platform import build_registry
    from core.diagnostic_registry import registry_status
    try:
        result['Components']=registry_status(build_registry(BASE_DIR))
    except Exception as exc:
        result['Components']=dict(source='component_registry',status='UNAVAILABLE',error=str(exc),pitch_rmvpe_available=False)
    return result

if __name__=='__main__':
    print(json.dumps(diagnostics(),ensure_ascii=False,indent=2))

