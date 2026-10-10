"""Project rights inventory from explicit evidence records, never from DSP success."""
import os


def dependency_evidence(project):
    """Human-readable evidence from this job's frozen records, not live imports."""
    versions, identities = {}, {}
    for stage in project.get('stage_records', []):
        parameters=stage.get('parameters') or {}
        name=stage.get('stage_name','unknown')
        for package,version in (parameters.get('package_versions') or {}).items():
            versions.setdefault((str(package),str(version)),set()).add(name)
        for artifact,digest in (parameters.get('dependency_hashes') or {}).items():
            identities.setdefault((str(artifact),str(digest)),set()).add(name)
    def escape(value):
        return value.replace('|','\\|').replace('\r',' ').replace('\n',' ')
    rows=['\n## 本任务依赖证据\n',
          '以下来自本任务冻结的 Stage 记录；未知版本不推定为已确认。SHA-256 识别实际依赖，不代表音质或授权通过。\n',
          '| 依赖 | 冻结版本 | Stage |','|---|---|---|']
    for (package,version),stages in sorted(versions.items()):
        rows.append('| {} | {} | {} |'.format(escape(package),escape(version),', '.join(sorted(stages))))
    if not versions:rows.append('| 未记录 | 未确认 | 不补造当前环境版本 |')
    rows.extend(['\n| 依赖文件/组件 | SHA-256 | Stage |','|---|---|---|'])
    for (artifact,digest),stages in sorted(identities.items()):
        rows.append('| {} | {} | {} |'.format(escape(artifact),escape(digest),', '.join(sorted(stages))))
    if not identities:rows.append('| 未记录 | 未确认 | 不补造当前环境指纹 |')
    return '\n'.join(rows)+'\n'


def license_manifest(store, mode, model_key, model_name, source_path, source_hash):
    def record(kind, key):
        rows = store.list_licenses(kind,key)
        if len(rows) == 1 and not rows[0].get('conflict'):
            return rows[0]
        return dict(subject_type=kind,subject_id=key,commercial_status='Unknown',note='未记录完整授权证据或存在冲突')
    source = record('input',source_hash)
    required = [source]
    model = record('model',model_key) if mode == 'voice_conversion' else None
    voice = record('voice',model_key) if mode == 'voice_conversion' else None
    if model: required += [model,voice]
    states = [r.get('commercial_status','Unknown') for r in required]
    overall = 'Ready' if all(s == 'Ready' for s in states) else ('Warning' if 'Warning' in states else 'Unknown')
    return dict(commercial_status=overall,
                rvc_model=dict(id=model_key,name=model_name,**{k:model.get(k) for k in ('commercial_status','license_name','source_url','author','note')}) if model else None,
                voice_rights=voice,input_rights=source,samples=[],
                input_source=os.path.basename(source_path),source_sha256=source_hash,
                notes='状态仅基于用户记录的证据完整性；处理成功不代表取得声音、模型或输入授权。未参与项目的样本档案不自动关联。')
