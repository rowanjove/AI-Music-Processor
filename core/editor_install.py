"""Pinned external editor installation outside the immutable program tree."""
import argparse,base64,hashlib,json,os,shutil,stat,subprocess,sys,tempfile,uuid,zipfile
from pathlib import Path
if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.component_location import save_editor_location,_plain_ancestors
from core.download_manager import download_verified

HOSTS=['github.com','objects.githubusercontent.com','github-releases.githubusercontent.com','release-assets.githubusercontent.com']

def _run_installer(command):
    try:return subprocess.run(command,timeout=900,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).returncode
    except OSError as exc:
        if os.name!='nt' or getattr(exc,'winerror',None)!=740:raise
        # Windows presents its standard UAC consent for this pinned installer
        # only.  The application Python/helper itself remains unprivileged.
        literal=lambda text:"'"+text.replace("'","''")+"'"
        script="$p=Start-Process -FilePath %s -ArgumentList %s -Verb RunAs -Wait -PassThru -ErrorAction Stop; exit $p.ExitCode"%(literal(command[0]),literal(subprocess.list2cmdline(command[1:])))
        encoded=base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        shell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        return subprocess.run([str(shell),'-NoProfile','-EncodedCommand',encoded],timeout=900,creationflags=subprocess.CREATE_NO_WINDOW).returncode

def install_editor(root,data_root,kind,cache_only=False,progress=None):
    root=Path(root).resolve();data=_plain_ancestors(data_root).resolve()
    try:data.relative_to(root)
    except ValueError:pass
    else:raise ValueError('编辑器和配置必须安装在应用目录之外')
    sources=json.loads((root/'packaging/editor-sources.json').read_text(encoding='utf-8'))['editors']
    entry=sources[kind]
    if progress:progress({'event':'download','tool':kind,'version':entry['version']})
    cache=data/'component-cache';cache.mkdir(parents=True,exist_ok=True)
    archive=cache/entry['filename']
    download_verified([entry['url']],str(archive),entry['sha256'],HOSTS,max_bytes=entry['size'],progress=None)
    if archive.stat().st_size!=entry['size']:raise ValueError('编辑器下载大小不匹配')
    if cache_only:return {'status':'CACHED_NOT_INSTALLED','tool':kind,'sha256':entry['sha256']}
    tools=data/'external-tools';tools.mkdir(parents=True,exist_ok=True)
    if tools.is_symlink() or getattr(tools.lstat(),'st_file_attributes',0)&0x400:raise ValueError('编辑器目录不能包含重解析点')
    stage=Path(tempfile.mkdtemp(prefix='.'+kind+'-',dir=str(tools)))
    final=tools/(kind+'-'+entry['version']+'-'+uuid.uuid4().hex[:8]);committed=False
    try:
        if entry['strategy']=='zip':
            with zipfile.ZipFile(archive) as z:
                if len(z.infolist())>20000 or sum(i.file_size for i in z.infolist())>2*1024**3:raise ValueError('编辑器归档超过限制')
                seen=set()
                for row in z.infolist():
                    parts=row.filename.rstrip('/').split('/')
                    if '\\' in row.filename or ':' in row.filename or any(p in ('','.','..') for p in parts) or stat.S_ISLNK(row.external_attr>>16):raise ValueError('不安全的编辑器归档路径')
                    key=row.filename.casefold()
                    if key in seen:raise ValueError('编辑器归档路径重复')
                    seen.add(key)
                    target=stage.joinpath(*parts)
                    if row.is_dir():target.mkdir(parents=True,exist_ok=True);continue
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(row) as src,target.open('xb') as dst:shutil.copyfileobj(src,dst)
        else:
            if entry['strategy']=='msi':
                command=[str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/msiexec.exe'),'/a',str(archive),'/qn','TARGETDIR='+str(stage)]
            elif entry['strategy']=='nsis':command=[str(archive),'/S','/D='+str(stage)]
            elif entry['strategy']=='inno':command=[str(archive),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/CURRENTUSER','/DIR='+str(stage)]
            else:raise ValueError('未知编辑器安装策略')
            if progress:progress({'event':'install','tool':kind})
            code=_run_installer(command)
            if code not in (0,3010):raise RuntimeError('编辑器安装失败：'+str(code))
        candidates=[p for p in stage.rglob('*.exe') if p.name.casefold() in entry['executables']]
        if len(candidates)!=1:raise ValueError('安装结果没有唯一的编辑器主程序')
        relative=candidates[0].relative_to(stage)
        # Reuse the app's PE/path validation before moving or changing settings.
        from core.component_location import _editor_path
        _editor_path(str(candidates[0]))
        for p in stage.rglob('*'):
            if p.is_symlink() or getattr(p.lstat(),'st_file_attributes',0)&0x400:raise ValueError('编辑器安装结果包含重解析点')
        os.replace(stage,final);committed=True
        location=save_editor_location(data/'config',kind,str(final/relative))
        result={'status':'INSTALLED','tool':kind,'version':entry['version'],'source_sha256':entry['sha256'],'location':location,'audio_import_status':'READY_FOR_EXTERNAL_VALIDATION'}
        receipt=data/'logs'/('editor-'+kind+'-install.json');receipt.parent.mkdir(parents=True,exist_ok=True)
        try:receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError as exc:result['receipt_warning']=str(exc)
        return result
    except Exception:
        # A failed registration leaves the previous editor settings intact.
        if committed:shutil.rmtree(final,ignore_errors=True)
        raise
    finally:
        if stage.exists():shutil.rmtree(stage,ignore_errors=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--data-root',required=True);parser.add_argument('--tools',required=True);parser.add_argument('--cache-only',action='store_true')
    args=parser.parse_args()
    try:
        names=['audacity','lmms','musescore','neuralnote'] if args.tools=='editors' else [n.removeprefix('tool.') for n in args.tools.split(',')]
        for name in names:print(json.dumps(install_editor(args.root,args.data_root,name,args.cache_only,lambda x:print(json.dumps(x,ensure_ascii=False),flush=True)),ensure_ascii=False),flush=True)
        return 0
    except Exception as exc:print(json.dumps({'status':'FAILED','error':str(exc)},ensure_ascii=False),flush=True);return 1

if __name__=='__main__':raise SystemExit(main())
