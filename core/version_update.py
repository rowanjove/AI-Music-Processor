"""Install immutable versions side by side; select only after a full runtime gate.

Selection is metadata, never an in-place runtime update. The GUI may launch the
returned launcher after ending its current session; this module launches none.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path

from core.component_location import (
    ComponentLocationError, _assert_plain_tree, _plain_ancestors, _read_manifest,
    _same_or_nested, _safe_relative, _validate_source_files, install_verified_bundle,
)

SELECTION_NAME = 'version-selection.json'
MAX_RECORD_BYTES = 64 * 1024
ACCEPTED = ('PASS', 'PASS_WITH_WARNINGS')


class VersionUpdateError(RuntimeError):
    pass


def _version_path(path):
    candidate = Path(os.fspath(path))
    if not candidate.is_absolute():
        raise VersionUpdateError('版本及配置路径必须为绝对路径')
    for part in candidate.parts[1:]:
        _safe_relative(part)
    return _plain_ancestors(candidate)


def _checkpoint(cancel):
    from core.processes import checkpoint
    checkpoint()
    if cancel is not None and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()):
        raise VersionUpdateError('版本操作已取消；当前选择保持不变')


def _read_bytes(path):
    path = _plain_ancestors(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise VersionUpdateError('版本选择文件类型或大小无效')
    return path.read_bytes()


def _selection(raw):
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise VersionUpdateError('版本选择记录损坏') from exc
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise VersionUpdateError('未知版本选择schema')
    for key in ('selected', 'previous'):
        entry = value.get(key)
        if not isinstance(entry, dict) or set(entry) != {'root', 'manifest_sha256'}:
            raise VersionUpdateError('版本选择身份不完整')
        digest = entry['manifest_sha256']
        if (not isinstance(entry['root'], str) or not Path(entry['root']).is_absolute()
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise VersionUpdateError('版本选择身份无效')
    if value.get('verification_status') not in ACCEPTED:
        raise VersionUpdateError('版本选择记录未通过完整验证')
    return value


def _atomic_json(path, value):
    _plain_ancestors(path)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    temporary = path.with_name(path.name+'.part.'+uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _plain_ancestors(path)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def _operation_lock(config):
    config = _version_path(config)
    config.mkdir(parents=True, exist_ok=True)
    if not config.is_dir():
        raise VersionUpdateError('配置路径不是普通目录')
    path = _plain_ancestors(config/'.version-selection.lock')
    stream = path.open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield config
    except OSError as exc:
        raise VersionUpdateError('版本操作失败或另一版本操作正在执行') from exc
    finally:
        if acquired:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _identity(root, verify_files=True):
    root = _version_path(root)
    _assert_plain_tree(root)
    document, records = _read_manifest(root)
    if type(document.get('schema_version')) is not int or type(document.get('file_count')) is not int:
        raise VersionUpdateError('版本清单schema及文件计数必须是整数')
    if verify_files:
        _validate_source_files(root, records)
    launcher = root/'AI-Music-Processor.exe'
    if not launcher.is_file() or not any(r['path'] == launcher.name for r in records):
        raise VersionUpdateError('完整版本清单缺少Launcher')
    return {'root': str(root), 'manifest_sha256': hashlib.sha256((root/'release-manifest.json').read_bytes()).hexdigest()}


def _full_gate(root, config, evidence, verifier, cancel, progress):
    default = verifier is None
    if default:
        from core.runtime_verify import verify_runtime
        verifier = verify_runtime
    data = _plain_ancestors(config.parent/'temp'/('version-verification-'+uuid.uuid4().hex))
    if _same_or_nested(data, root):
        raise VersionUpdateError('完整校验数据目录必须位于版本目录之外')
    _checkpoint(cancel)
    def on_progress(event):
        _checkpoint(cancel)
        if progress is not None:
            progress(event)
    result = verifier(root=root, fast=False, data_root=data, progress=on_progress)
    _checkpoint(cancel)
    evidence['verification_kind'] = 'CURRENT_TRUSTED_FULL_RUNTIME_VERIFIER' if default else 'INJECTED_VERIFIER_NOT_RELEASE_PROOF'
    evidence['verification'] = result
    if (not isinstance(result, dict) or result.get('status') not in ACCEPTED
            or os.path.normcase(os.path.abspath(result.get('root') or '')) != os.path.normcase(str(root))):
        raise VersionUpdateError('新选择未通过FULL Runtime校验')
    return result


def _execute(config_dir, action, operation, cancel):
    with _operation_lock(config_dir) as config:
        selection_path = config/SELECTION_NAME
        before = _read_bytes(selection_path)
        evidence = {'schema_version': 1, 'action': action, 'status': 'RUNNING',
                    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'previous_selection_sha256': hashlib.sha256(before).hexdigest() if before else None,
                    'user_data_copied': False, 'live_files_modified': False}
        receipt = config/('version-'+action+'-'+uuid.uuid4().hex+'.json')
        try:
            prior = _selection(before)
            record, launcher = operation(config, prior, evidence)
            if _read_bytes(selection_path) != before:
                raise VersionUpdateError('版本选择在操作期间变化，拒绝覆盖')
            evidence['status'] = 'VERIFIED_READY'
            evidence['selection'] = record
            _atomic_json(receipt, evidence)
            record['evidence_path'] = str(receipt)
            _checkpoint(cancel)
            if _read_bytes(selection_path) != before:
                raise VersionUpdateError('版本选择在提交前变化，拒绝覆盖')
            _atomic_json(selection_path, record)
            return {'status': 'VERIFIED_READY', 'launcher_path': str(launcher),
                    'selected_root': record['selected']['root'], 'previous_root': record['previous']['root'],
                    'verification_status': record['verification_status'], 'evidence_path': str(receipt),
                    'verification_kind': evidence['verification_kind'], 'launched': False}
        except Exception as exc:
            evidence['status'] = 'FAILED'
            evidence['error'] = type(exc).__name__+': '+str(exc)
            try:
                _atomic_json(receipt, evidence)
            except Exception as receipt_error:
                raise VersionUpdateError('版本失败且无法保存失败证据；选择未提交：'+str(receipt_error)) from exc
            raise VersionUpdateError(str(exc)+'；失败证据：'+str(receipt)) from exc


def install_version_update(current_root, new_source, target_root, config_dir, *, verifier=None,
                           cancel=None, progress=None, in_use=False):
    """Copy to a new root, FULL verify, then record selected/previous identities."""
    def operation(config, prior, evidence):
        if in_use:
            raise VersionUpdateError('运行中的任务不能切换版本选择')
        current, source, target = map(_version_path, (current_root, new_source, target_root))
        if any(_same_or_nested(config, root) for root in (current, source, target)):
            raise VersionUpdateError('配置目录必须位于所有版本目录之外')
        if _same_or_nested(current, target) or _same_or_nested(current, source):
            raise VersionUpdateError('新版本源及目标不得覆盖或嵌入当前版本')
        _checkpoint(cancel)
        old = _identity(current)
        if prior is not None and prior['selected'] != old:
            raise VersionUpdateError('当前运行版本与已选择身份不同，拒绝更新')
        evidence.update(current_root=str(current), previous_root=str(current), new_root=str(target))
        def on_copy(event):
            _checkpoint(cancel)
            if progress is not None:
                progress(event)
        evidence['installation'] = install_verified_bundle(source, target, progress=on_copy)
        new = _identity(target)
        result = _full_gate(target, config, evidence, verifier, cancel, progress)
        if _identity(target) != new or _identity(current) != old:
            raise VersionUpdateError('完整校验期间版本身份变化')
        _checkpoint(cancel)
        return ({'schema_version': 1, 'action': 'update', 'selected': new, 'previous': old,
                 'current_root': str(current), 'new_root': str(target),
                 'verification_status': result['status'], 'full_verification': verifier is None,
                 'verification_kind': evidence['verification_kind']}, target/'AI-Music-Processor.exe')
    return _execute(config_dir, 'update', operation, cancel)


def rollback_previous_version(config_dir, *, verifier=None, cancel=None, progress=None, in_use=False):
    """Revalidate previous immutable identity before changing selection metadata."""
    def operation(config, prior, evidence):
        if in_use:
            raise VersionUpdateError('运行中的任务不能回退版本选择')
        if prior is None:
            raise VersionUpdateError('没有可回退的版本记录')
        root = _version_path(prior['previous']['root'])
        selected = _version_path(prior['selected']['root'])
        if _same_or_nested(config, root) or _same_or_nested(config, selected):
            raise VersionUpdateError('配置目录不得嵌入版本目录')
        _checkpoint(cancel)
        old = _identity(root)
        if old != prior['previous']:
            raise VersionUpdateError('上一版本manifest身份变化')
        if _identity(selected) != prior['selected']:
            raise VersionUpdateError('当前选择的版本身份变化')
        evidence.update(current_root=str(selected), previous_root=str(root), new_root=str(root))
        result = _full_gate(root, config, evidence, verifier, cancel, progress)
        if _identity(root) != old or _identity(selected) != prior['selected']:
            raise VersionUpdateError('回退校验期间版本身份变化')
        _checkpoint(cancel)
        return ({'schema_version': 1, 'action': 'rollback', 'selected': old,
                 'previous': prior['selected'], 'current_root': str(selected), 'new_root': str(root),
                 'verification_status': result['status'], 'full_verification': verifier is None,
                 'verification_kind': evidence['verification_kind']}, root/'AI-Music-Processor.exe')
    return _execute(config_dir, 'rollback', operation, cancel)


# Staging means publishing a separate immutable tree and selecting it only
# after verification; callers remain responsible for closing and launching.
stage_update_bundle = install_version_update
