"""Versioned metadata-only .ampbackup export and atomic, reversible restore."""
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

SCHEMA_VERSION=1
MAX_BYTES=8*1024**2
MAX_PROFILES=256


def _json_bytes(value):
    data=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
    if len(data)>MAX_BYTES:raise ValueError('配置备份超过 8 MiB 安全上限')
    return data


def _safe_target(root,relative):
    root=Path(os.path.abspath(root))
    target=root/relative
    for part in [target,*target.parents]:
        if part==root.parent:break
        if os.path.lexists(part):
            info=part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                raise ValueError('配置导入拒绝链接或重解析点：'+str(part))
    return target


def _validate_payload(payload):
    if not isinstance(payload,dict):raise ValueError('配置备份内容必须是对象')
    required={'app_settings','profiles','component_selection','model_metadata'}
    if set(payload)!=required:raise ValueError('配置备份字段不完整或含未知数据类别')
    if not isinstance(payload['app_settings'],dict) or not isinstance(payload['profiles'],dict):
        raise ValueError('App Settings / Profiles 格式无效')
    if len(payload['profiles'])>MAX_PROFILES:raise ValueError('Profiles 数量超过安全上限')
    from core.config import _validate_model_profile, normalize_settings
    normalized=normalize_settings(payload['app_settings'],strict=True)
    payload=dict(payload,app_settings=normalized)
    names=set()
    for name,profile in payload['profiles'].items():
        if (not isinstance(name,str) or not 1<=len(name)<=80 or name.rstrip(' .')!=name or
                not any(letter.isalnum() for letter in name) or
                any(not (letter.isalnum() or letter in '_-. ') for letter in name)):
            raise ValueError('Profile 标识包含不安全的文件名字符')
        if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*('COM%d'%i for i in range(1,10)),*('LPT%d'%i for i in range(1,10))}:
            raise ValueError('Profile 标识是 Windows 保留名称')
        if name.casefold() in names:raise ValueError('Profile 文件名存在 Windows 大小写碰撞')
        names.add(name.casefold())
        _validate_model_profile(profile,'backup:'+name)
    if not isinstance(payload['component_selection'],list) or any(not isinstance(x,str) for x in payload['component_selection']):
        raise ValueError('组件选择必须是组件 ID 列表')
    if not isinstance(payload['model_metadata'],dict):raise ValueError('模型元数据格式无效')
    external=payload['app_settings'].get('external_editor_locations')
    if external is not None:
        if (not isinstance(external,dict) or type(external.get('schema_version')) is not int or
                external['schema_version']!=1 or not isinstance(external.get('editors'),dict)):
            raise ValueError('外部编辑器配置版本或格式无效')
        if len(_json_bytes(external))>64*1024:raise ValueError('外部编辑器配置超过安全上限')
    _json_bytes(payload)
    return payload


def export_settings_backup(destination,settings,profiles=None,component_selection=None,model_metadata=None):
    payload=_validate_payload(dict(app_settings=settings,profiles=profiles or {},
        component_selection=component_selection or [],model_metadata=model_metadata or {}))
    data=_json_bytes(payload)
    manifest={'format':'AI-Music-Processor-settings','schema_version':SCHEMA_VERSION,
        'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'payload_sha256':hashlib.sha256(data).hexdigest(),'contains_model_weights':False}
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with zipfile.ZipFile(temporary,'x',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json',_json_bytes(manifest))
            archive.writestr('payload.json',data)
        os.replace(temporary,destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'path':str(destination),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'contains_model_weights':False,'profiles':len(payload['profiles'])}


def read_settings_backup(source):
    with zipfile.ZipFile(source) as archive:
        if sorted(archive.namelist())!=['manifest.json','payload.json']:
            raise ValueError('配置备份只允许 manifest.json 与 payload.json；禁止提取任意文件')
        parts={}
        for name in ('manifest.json','payload.json'):
            member=archive.getinfo(name)
            if member.file_size>MAX_BYTES or member.flag_bits&1 or member.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
                raise ValueError('配置备份压缩格式或大小不安全')
            with archive.open(member) as handle:data=handle.read(MAX_BYTES+1)
            if len(data)>MAX_BYTES:raise ValueError('配置备份解压内容超限')
            parts[name]=data
        manifest=json.loads(parts['manifest.json'].decode('utf-8'))
        if not isinstance(manifest,dict) or manifest.get('format')!='AI-Music-Processor-settings':
            raise ValueError('这不是 AI Music Processor 配置备份')
        version=manifest.get('schema_version')
        if type(version) is not int or version not in (0,1):raise ValueError('未知配置备份版本，未修改当前设置')
        if hashlib.sha256(parts['payload.json']).hexdigest()!=manifest.get('payload_sha256'):
            raise ValueError('配置备份 SHA-256 不匹配')
        if manifest.get('contains_model_weights') is not False:raise ValueError('禁止通过配置导入模型权重')
        payload=json.loads(parts['payload.json'].decode('utf-8'))
        # v0 kept App Settings under the old "settings" name. Preserve unknown
        # preference keys; do not claim to migrate unsupported future formats.
        if version==0 and isinstance(payload,dict) and 'settings' in payload:
            payload=dict(payload);payload['app_settings']=payload.pop('settings')
        return {'payload':_validate_payload(payload),'source_version':version,'target_version':SCHEMA_VERSION}


def _restore_files(payload):
    files={'app.json':payload['app_settings'],'component-selection.json':payload['component_selection'],
           'model-metadata.json':payload['model_metadata']}
    files.update({'model-profiles/'+name+'.json':value for name,value in payload['profiles'].items()})
    if 'external_editor_locations' in payload['app_settings']:
        files['external-editors.json']=payload['app_settings']['external_editor_locations']
    return files


def plan_settings_restore(source,config_directory):
    read=read_settings_backup(source);payload=read['payload'];root=Path(config_directory)
    files=_restore_files(payload)
    changes=[]
    for relative,value in sorted(files.items()):
        target=_safe_target(root,relative);data=_json_bytes(value)
        if target.exists() and not target.is_file():raise ValueError('配置目标不是普通文件：'+str(target))
        old=target.read_bytes() if target.exists() else None
        if old!=data:changes.append({'relative_path':relative,'previous_sha256':hashlib.sha256(old).hexdigest() if old is not None else None,
                                    'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
    return {**read,'changes':changes,'writes_performed':False,'model_weights_restored':False}


def restore_settings_backup(source,config_directory,expected_plan=None):
    plan=plan_settings_restore(source,config_directory)
    if expected_plan is not None and plan!=expected_plan:raise ValueError('预览后备份或当前配置已变化，请重新预览')
    root=Path(os.path.abspath(config_directory));root.mkdir(parents=True,exist_ok=True)
    receipt=_safe_target(root,'backup-history')/uuid.uuid4().hex
    receipt.mkdir(parents=True,exist_ok=False)
    payload=plan['payload']
    files=_restore_files(payload)
    originals,staged,committed={},{},[]
    try:
        for change in plan['changes']:
            name=change['relative_path'];target=_safe_target(root,name)
            target.parent.mkdir(parents=True,exist_ok=True)
            previous=target.read_bytes() if target.exists() else None
            if previous is not None:
                backup=receipt/name;backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(previous)
            originals[name]=previous
            temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
            staged[name]=temporary
            with temporary.open('xb') as handle:handle.write(_json_bytes(files[name]))
        for name,temporary in staged.items():
            target=_safe_target(root,name)
            current=target.read_bytes() if target.exists() else None
            if current!=originals[name]:raise ValueError('提交期间当前配置发生变化，导入已回滚')
            os.replace(temporary,target);committed.append(name)
        (receipt/'restore.json').write_bytes(_json_bytes({'changes':plan['changes'],'source_version':plan['source_version']}))
    except Exception:
        try:
            for name in reversed(committed):
                target=_safe_target(root,name)
                if originals[name] is None:target.unlink()
                else:
                    recover=target.with_name(target.name+'.restore-'+uuid.uuid4().hex+'.tmp')
                    recover.write_bytes(originals[name]);os.replace(recover,target)
        except OSError as error:
            raise RuntimeError('配置回滚失败；原文件已保留于 '+str(receipt)) from error
        raise
    finally:
        for path in staged.values():path.unlink(missing_ok=True)
    return {'status':'RESTORED','backup_directory':str(receipt),'changes':plan['changes'],'model_weights_restored':False}
