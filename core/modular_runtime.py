"""Integrity verification for an explicitly marked, installed Polish-only kit."""
import hashlib,json,os,subprocess,time,uuid
from pathlib import Path,PurePosixPath

PROFILE='modular-polish-v1'
MODULES=('numpy','scipy','soundfile')


def verify_modular_runtime(root,data_root,timeout=900,progress=None,cancel_event=None):
    root=Path(root).resolve(strict=True);data_root=Path(data_root).resolve()
    result={'status':'FAIL','root':str(root),'data_root':str(data_root),'profile':PROFILE,'allowed_modes':['polish'],'checks':{}}
    deadline=time.monotonic()+timeout
    def checkpoint():
        if cancel_event is not None and cancel_event.is_set():raise RuntimeError('runtime verification cancelled')
        if time.monotonic()>deadline:raise TimeoutError('modular runtime verification timed out')
    def path(relative):
        if not isinstance(relative,str) or not relative or '\\' in relative or ':' in relative:
            raise ValueError('invalid modular path')
        parts=relative.split('/')
        if any(p in ('','.','..') for p in parts):raise ValueError('invalid modular path')
        target=root.joinpath(*parts)
        target.resolve().relative_to(root)
        current=root
        for part in parts:
            current=current/part
            if current.is_symlink() or (current.exists() and getattr(current.lstat(),'st_file_attributes',0)&0x400):raise ValueError('modular path reparse point')
        return target
    try:
        checkpoint()
        marker=root/'LIGHTWEIGHT_RELEASE'
        if not marker.is_file() or marker.read_text(encoding='utf-8-sig').strip()!='AI Music Processor Lightweight Release v1':raise ValueError('missing lightweight marker')
        manifest_path=path('runtime-manifest/bootstrap-runtime.json')
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('schema_version')!=1 or manifest.get('profile')!=PROFILE or manifest.get('allowed_modes')!=['polish']:raise ValueError('invalid modular profile')
        files=manifest.get('files');python=manifest.get('python');tools=manifest.get('tools')
        if not isinstance(files,list) or not files or not isinstance(python,dict) or not isinstance(tools,dict):raise ValueError('incomplete modular manifest')
        by_path={};last=time.monotonic()
        for index,row in enumerate(files,1):
            checkpoint();relative=row['path']
            if relative.casefold() in by_path:raise ValueError('duplicate modular path')
            target=path(relative)
            if not target.is_file() or target.stat().st_size!=row['size']:raise ValueError('modular file missing or size changed: '+relative)
            before=target.stat();digest=hashlib.sha256()
            with target.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):checkpoint();digest.update(chunk)
            after=target.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns) or digest.hexdigest()!=row['sha256']:raise ValueError('modular SHA mismatch: '+relative)
            by_path[relative.casefold()]=row
            if progress and time.monotonic()-last>1:progress('已校验 %d/%d 个运行文件'%(index,len(files)));last=time.monotonic()
        required={'ai_music_processor.pyw','core/pipeline.py','core/modular_runtime.py',python['executable'].casefold(),'runtime/bin/ffmpeg.exe','runtime/bin/ffprobe.exe'}
        if not required.issubset(by_path):raise ValueError('required modular file omitted')
        # Extra importable code cannot silently join an already sealed runtime.
        for prefix in ('core','tools/RVC/RVC-beta-v2-0618/runtime','runtime/bin'):
            base=path(prefix)
            if not base.is_dir():raise ValueError('missing modular directory')
            for target in base.rglob('*'):
                checkpoint()
                relative=target.relative_to(root).as_posix()
                path(relative)
                if target.is_file() and relative.casefold() not in by_path:
                    raise ValueError('unmanifested modular file: '+relative)
        executable=path(python['executable'])
        code="import json,platform,importlib;print(json.dumps({'version':platform.python_version(),'modules':{n:importlib.import_module(n).__file__ for n in ('numpy','scipy','soundfile')}}))"
        if progress:progress('正在检查精修引擎和音频依赖…')
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8')
        def run(command):
            checkpoint()
            proc=subprocess.Popen(command,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                while True:
                    checkpoint()
                    try:out,err=proc.communicate(timeout=.1);break
                    except subprocess.TimeoutExpired:pass
                if proc.returncode:raise RuntimeError(err.decode('utf-8',errors='replace')[-2500:])
                return out.decode('utf-8',errors='replace')
            finally:
                if proc.poll() is None:
                    if os.name=='nt':
                        try:
                            subprocess.run([str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/taskkill.exe'),'/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
                        except (OSError,subprocess.TimeoutExpired):pass
                    if proc.poll() is None:proc.kill()
                    proc.communicate(timeout=10)
        actual=json.loads(run([str(executable),'-B','-I','-c',code]))
        if actual['version']!=python['version']:raise ValueError('modular Python version changed')
        for name,origin in actual['modules'].items():
            relative=Path(origin).resolve().relative_to(root).as_posix()
            if relative.casefold() not in by_path or name not in MODULES:raise ValueError('external or unmanifested module')
        for name in ('ffmpeg','ffprobe'):
            text=run([str(path('runtime/bin/'+name+'.exe')),'-version']).splitlines()[0]
            if text!=tools[name]['version_line']:raise ValueError('modular tool version changed')
        data_root.mkdir(parents=True,exist_ok=True)
        probe=data_root/('.modular-write-'+uuid.uuid4().hex)
        try:
            with probe.open('x') as f:f.write('probe')
        finally:
            if probe.exists():probe.unlink()
        result['checks']={'inventory':{'status':'PASS','file_count':len(files)},'python':{'status':'PASS','version':actual['version']},'ffmpeg':{'status':'PASS'}}
        result['status']='PASS'
    except Exception as exc:result['error']=str(exc)
    return result
