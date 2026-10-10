"""Evidence inventory, not a legal approval or inferred redistribution grant."""
import email,hashlib,json,os,re,stat,subprocess
from pathlib import Path,PurePosixPath

KIND='AI_MUSIC_PROCESSOR_LOCAL_LICENSE_DOSSIER_V1'

def _license_name(name):
    name=name.casefold()
    if PurePosixPath(name).suffix in ('.py','.pyw','.pyc','.pyo','.pyx','.c','.cpp','.h','.js','.ts','.exe','.dll','.so','.png','.jpg'):
        return False
    return name.startswith(('license','copying','notice','copyright')) or name.endswith('-license.txt')

def _sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def _file(root,relative):
    p=PurePosixPath(str(relative).replace('\\','/'))
    if p.is_absolute() or not p.parts or any(x in ('','..','.') or ':' in x for x in p.parts):raise ValueError('Unsafe license evidence path')
    target=root.joinpath(*p.parts)
    for item in (target,*target.parents):
        if item==root.parent:break
        if os.path.lexists(item):
            info=item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:raise ValueError('Linked license evidence path')
    return target

def _read(root,name):
    return json.loads(_file(root,'runtime-manifest/'+name).read_text(encoding='utf-8'))

def _subjects(root,inventory=None):
    packages=_read(root,'packages.lock.json')['distributions']
    models=_read(root,'models.json')['assets']
    result=[('python:'+p['normalized_name'],p) for p in packages]+[('model:'+m['id'],m) for m in models]
    runtime_path=root/'runtime-manifest/runtime.json'
    if runtime_path.is_file():
        python=_read(root,'runtime.json')['python']
        result.append(('tool:cpython',{'name':'CPython runtime','version':python['version'],'prefixes':['tools/RVC/RVC-beta-v2-0618/runtime/']}))
    for row in inventory if inventory is not None else _read(root,'inventory.json')['files']:
        if row['path'].casefold()=='runtime/bin/ffmpeg.exe':
            result.append(('tool:ffmpeg',{'name':'Embedded FFmpeg/FFprobe','version':'binary identity in runtime inventory','prefixes':['runtime/bin/']}))
    vendor_path=root/'runtime-manifest/vendor.json'
    if vendor_path.is_file():
        result.append(('tool:rvc-vendor',{'name':'RVC 0618 vendor code and notices','version':'0618','prefixes':['tools/RVC/RVC-beta-v2-0618/']}))
    return result

def _subject_entry(root,identifier,row,evidence,inventory_map):
    if identifier.startswith('python:'):
        folder=row['metadata_root'];relative=folder+'/METADATA';meta=_file(root,relative)
        pinned=inventory_map.get(relative.casefold())
        if pinned is not None and not meta.is_file():
            raise ValueError('License metadata source required by trusted inventory is missing')
        if meta.is_file():
            if pinned is None or meta.stat().st_size>8*1024*1024 or _sha(meta)!=pinned['sha256']:
                raise ValueError('License metadata differs from trusted inventory')
            parsed=email.message_from_string(meta.read_text(encoding='utf-8',errors='replace'))
        else:parsed={}
        refs=[value for key,value in evidence.items() if key.casefold().startswith(folder.casefold().rstrip('/')+'/')]
        entry={'id':identifier,'name':row['name'],'version':row.get('version'),'declared_license':parsed.get('License','UNKNOWN'),'source':parsed.get('Home-page','UNKNOWN'),'author':parsed.get('Author','UNKNOWN'),'assets':[],'evidence':refs}
    elif identifier.startswith('model:'):
        entry={'id':identifier,'name':row['id'],'version':None,'declared_license':row.get('license_status','UNKNOWN'),'source':row.get('source_url','UNKNOWN'),'author':row.get('author','UNKNOWN'),'assets':row['files'],'evidence':[]}
    else:
        refs=[value for key,value in evidence.items() if any(key.casefold().startswith(prefix.casefold()) for prefix in row['prefixes'])]
        entry={'id':identifier,'name':row['name'],'version':row['version'],'declared_license':'See original notices; approval not inferred','source':'UNKNOWN','author':'UNKNOWN','assets':[],'evidence':refs}
    entry.update(redistributable='UNKNOWN',commercial='UNKNOWN',approval='NOT_INFERRED',license_text_status='PRESENT' if entry['evidence'] else 'NOT_LOCATED')
    if identifier=='tool:ffmpeg':entry['license_text_status']='BINARY_LICENSE_REPORT_PRESENT'
    return entry

def render_license_index(subjects):
    rows=['# 第三方许可与来源清单','','本清单从当前 runtime 元数据、原始许可文本和模型身份生成。文本存在不等于获准再分发或商用；UNKNOWN 不提升为授权。当前交付用途为本地验收，公共发布门禁关闭。','','完整原文及 SHA 见 `LICENSES/manifest.json` 与 `LICENSES/texts/`；FFmpeg 原生 -L 输出见 `LICENSES/ffmpeg-build-license.txt`。缺失的模型许可原文明确保留 NOT_LOCATED，不用 RVC 源码许可替代模型授权。','','| 资产 | 版本 | 原文证据数 | 再分发 / 商用 |','|---|---|---|---|']
    rows.extend('| {} | {} | {} | UNKNOWN / UNKNOWN |'.format(s['name'].replace('|',' '),s['version'] or '模型身份见 manifest',len(s['evidence'])) for s in subjects)
    return '\n'.join(rows)+'\n'

def generate_license_dossier(root):
    root=Path(root).resolve(strict=True);destination=_file(root,'LICENSES')
    if destination.exists():raise FileExistsError('Preserve existing license dossier: '+str(destination))
    destination.mkdir();texts=destination/'texts';texts.mkdir()
    subjects=[];evidence={}
    # Inventory-backed license/notice files include transitive/native notices.
    inventory=_read(root,'inventory.json')['files']
    for row in inventory:
        relative=row['path'];name=PurePosixPath(relative).name.casefold()
        if not _license_name(name):continue
        source=_file(root,relative)
        if source.stat().st_size>8*1024*1024:raise ValueError('License text exceeds bounded limit')
        digest=_sha(source)
        if digest!=row['sha256']:raise ValueError('License source differs from trusted runtime inventory: '+relative)
        output=texts/(digest+'.txt')
        if not output.exists():output.write_bytes(source.read_bytes())
        evidence[relative]={'source_path':relative,'sha256':digest,'size':source.stat().st_size,'text_path':'LICENSES/texts/'+digest+'.txt'}
    inventory_map={r['path'].casefold():r for r in inventory}
    for identifier,row in _subjects(root,inventory):
        subjects.append(_subject_entry(root,identifier,row,evidence,inventory_map))
    native_reports=[]
    for row in inventory:
        if row['path'].casefold()!='runtime/bin/ffmpeg.exe':continue
        executable=_file(root,row['path'])
        if _sha(executable)!=row['sha256']:raise ValueError('FFmpeg license probe binary differs from inventory')
        process=subprocess.run([str(executable),'-L'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        content=process.stdout+process.stderr
        if process.returncode or not content or len(content)>1024*1024:raise ValueError('FFmpeg license probe failed')
        output=destination/'ffmpeg-build-license.txt';output.write_bytes(content)
        native_reports.append({'source_path':row['path'],'source_sha256':row['sha256'],'text_path':'LICENSES/ffmpeg-build-license.txt','sha256':_sha(output),'size':len(content),'command':['-L']})
        next(s for s in subjects if s['id']=='tool:ffmpeg')['license_text_status']='BINARY_LICENSE_REPORT_PRESENT'
    record={'schema_version':1,'kind':KIND,'approval':'NOT_INFERRED','public_release_allowed':False,'subjects':subjects,'texts':list(evidence.values()),'native_license_reports':native_reports,'scope':'bundled runtime distributions, native notices and model assets; optional external applications are not bundled'}
    (destination/'manifest.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    _file(root,'THIRD_PARTY_LICENSES.md').write_text(render_license_index(subjects),encoding='utf-8')
    return verify_license_dossier(root)

def verify_license_dossier(root,*,public_release=False):
    root=Path(root).resolve(strict=True)
    record=json.loads(_file(root,'LICENSES/manifest.json').read_text(encoding='utf-8'))
    if record.get('kind')!=KIND or record.get('schema_version')!=1 or record.get('approval')!='NOT_INFERRED' or record.get('public_release_allowed') is not False:raise ValueError('License dossier cannot self-approve legal rights')
    if not _file(root,'THIRD_PARTY_LICENSES.md').is_file():raise ValueError('Missing third-party license index')
    inventory=_read(root,'inventory.json')['files']
    subjects=record['subjects'];actual={s['id']:s for s in subjects};expected=dict(_subjects(root,inventory))
    if len(actual)!=len(subjects) or set(actual)!=set(expected):raise ValueError('License subject coverage mismatch')
    for identifier,row in expected.items():
        s=actual[identifier]
        if identifier.startswith('python:'):
            if s['name']!=row['name'] or s['version']!=row.get('version'):raise ValueError('License package identity mismatch')
        elif identifier.startswith('model:'):
            if s['assets']!=row['files']:raise ValueError('License model identity mismatch')
        elif s['name']!=row['name'] or s['version']!=row['version']:raise ValueError('License tool identity mismatch')
        if s['redistributable']!='UNKNOWN' or s['commercial']!='UNKNOWN' or s['approval']!='NOT_INFERRED':raise ValueError('Unverified license approval')
    texts=record['texts'];seen=set()
    for e in texts:
        key=e['source_path'].casefold()
        if key in seen:raise ValueError('Duplicate license text source')
        seen.add(key)
        for path in (e['source_path'],e['text_path']):
            target=_file(root,path)
            if target.stat().st_size!=e['size'] or _sha(target)!=e['sha256']:raise ValueError('License evidence identity mismatch: '+path)
    expected_texts={r['path'].casefold():r for r in inventory if _license_name(PurePosixPath(r['path']).name)}
    if seen!=set(expected_texts):raise ValueError('License text coverage mismatch')
    for e in texts:
        pinned=expected_texts[e['source_path'].casefold()]
        if e['sha256']!=pinned['sha256'] or e['size']!=pinned['size']:raise ValueError('License text differs from pinned inventory')
    binaries={r['path'].casefold():r for r in inventory if r['path'].casefold()=='runtime/bin/ffmpeg.exe'}
    reports=record['native_license_reports']
    if len(reports)!=len(binaries) or {r['source_path'].casefold() for r in reports}!=set(binaries):raise ValueError('Native license report coverage mismatch')
    for r in reports:
        pinned=binaries[r['source_path'].casefold()]
        if r['source_sha256']!=pinned['sha256'] or _sha(_file(root,r['source_path']))!=pinned['sha256']:raise ValueError('Native license binary identity mismatch')
        text=_file(root,r['text_path'])
        if text.stat().st_size!=r['size'] or _sha(text)!=r['sha256'] or r['command']!=['-L']:raise ValueError('Native license report identity mismatch')
    evidence={r['source_path']:r for r in texts}
    inventory_map={r['path'].casefold():r for r in inventory}
    for identifier,row in expected.items():
        if actual[identifier]!=_subject_entry(root,identifier,row,evidence,inventory_map):raise ValueError('License subject description/evidence mismatch')
    if _file(root,'THIRD_PARTY_LICENSES.md').read_text(encoding='utf-8')!=render_license_index(subjects):raise ValueError('License index differs from verified subjects')
    return {'status':'FAIL' if public_release else 'PASS_WITH_WARNINGS','subjects':len(subjects),'license_texts':len(texts),'public_release_allowed':False,'rights_status':'UNKNOWN','blockers':['Unverified redistribution/commercial grants'] if public_release else [],'manifest_sha256':_sha(_file(root,'LICENSES/manifest.json'))}
