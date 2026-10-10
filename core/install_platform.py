"""Local install plan, transfer, and repair. No network and no audio pipeline.

URLs are not invented. A failed copy does not replace a live file. User voice
weights are not repair targets.
"""
import json
import os
import shutil
import stat
import hashlib
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from core.component_platform import dependency_closure
from core.paths import application_data_paths
from core.storage import sha256_file

MARKER_VERSION = 1
MARKER_NAME = "first-run.json"
EXTERNAL = "READY_FOR_EXTERNAL_VALIDATION"
_MODE_IDS = {
    "polish": "mode.polish",
    "voice_conversion": "mode.voice_conversion",
    "transcribe": "mode.transcribe",
}
_RETRY_CAP = 2
_component_lock_guard = threading.Lock()
_component_lock_roots = set()


@contextmanager
def _component_install_lock(live_root):
    """Nonblocking transaction ownership shared by all instances of a root.

    Windows uses a Global named mutex, so separate data-root overrides cannot
    bypass ownership and the immutable release tree gains no lock files.
    """
    root = _plain_path(Path(live_root)).resolve()
    key = hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()
    busy = '另一个组件安装事务正在使用此应用目录，请完成后再重试'
    with _component_lock_guard:
        if key in _component_lock_roots:
            raise RuntimeError(busy)
        _component_lock_roots.add(key)
    handle = None
    acquired = False
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel.ReleaseMutex.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.CreateMutexW(None,False,'Global\\AI-Music-Processor-Components-'+key)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            result = kernel.WaitForSingleObject(handle,0)
            acquired = result in (0,0x80)
            if result == 0x102:
                raise RuntimeError(busy)
            if result == 0x80:
                raise RuntimeError('上一次组件事务异常退出；请先自检并用可信完整目录修复，不认定源树已经完整')
            if result != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            directory = _plain_path(Path('/tmp')/'ai-music-processor-component-locks')
            directory.mkdir(mode=0o700,exist_ok=True)
            path = _plain_path(directory/(key+'.lock'))
            handle = path.open('a+b')
            try:
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(busy) from error
            acquired = True
        yield
    finally:
        try:
            if handle is not None:
                if os.name == 'nt':
                    if acquired:kernel.ReleaseMutex(handle)
                    kernel.CloseHandle(handle)
                else:
                    if acquired:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
                    handle.close()
        finally:
            with _component_lock_guard:
                _component_lock_roots.discard(key)


def download_component_to_cache(urls, destination, expected_sha256, allowed_hosts, **options):
    """Network transport is separate from managed-file installation authority."""
    from core.download_manager import download_verified
    return download_verified(urls, destination, expected_sha256, allowed_hosts, **options)


def _plain_path(path):
    """Reject junctions/symlinks at every existing path ancestor before IO."""
    path = Path(os.path.abspath(os.fspath(path)))
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('组件路径不能经过符号链接或junction：' + str(candidate))
    return path


def _bounded_path(root, relative):
    _reject_escape(relative)
    base = _plain_path(root)
    target = _plain_path(base / Path(relative.replace('\\', '/')))
    if base not in target.parents:
        raise ValueError('组件路径越界')
    return str(target)


def _managed_targets(root):
    """Authorize writes from the active local inventory/catalog, not a donor."""
    from core.component_platform import catalog_path, load_catalog
    exact, directories, hashes = set(), [], {}
    inventory = _plain_path(Path(root) / 'runtime-manifest' / 'inventory.json')
    if inventory.is_file():
        with inventory.open(encoding='utf-8') as handle:
            rows = json.load(handle).get('files')
        if not isinstance(rows, list):
            raise ValueError('当前运行清单无效')
        for item in rows:
            if not isinstance(item, dict) or not isinstance(item.get('sha256'), str):
                raise ValueError('当前运行清单记录无效')
            relative = item.get('path')
            _reject_escape(relative)
            key = relative.replace('\\', '/').casefold()
            if key in hashes:
                raise ValueError('当前运行清单路径重复')
            exact.add(key)
            hashes[key] = item['sha256']
    else:
        catalog = _plain_path(catalog_path(root))
        if not catalog.is_file():
            raise ValueError('缺少当前组件目录或运行清单，拒绝无依据的离线写入')
        for component in load_catalog(catalog)['components']:
            if component['ownership'] != 'managed' or not component['implemented']:
                continue
            for probe in component['probes']:
                relative = probe.get('release_path') if probe['type'] == 'tool' else probe.get('path')
                if not relative:
                    continue
                _reject_escape(relative)
                if probe['type'] == 'relative_dir':
                    directories.append(relative.replace('\\', '/').casefold().rstrip('/') + '/')
                elif probe['type'] in ('relative_file', 'tool'):
                    exact.add(relative.replace('\\', '/').casefold())
    return exact, directories, hashes


def _authorize_managed(root, relative, targets):
    _reject_escape(relative)
    key = relative.replace('\\', '/').casefold()
    if (key.split('/')[0] in ('config', 'output', 'temp', 'logs', 'song', 'models', '处理后')
            or not (key.startswith(('core/', 'runtime/', 'tools/'))
                    or key in ('ai_music_processor.pyw', 'process_music.py', 'version'))):
        raise ValueError('离线安装不能写用户数据或未知应用路径：' + relative)
    exact, directories, hashes = targets
    if key not in exact and not any(key.startswith(prefix) for prefix in directories):
        raise ValueError('文件不属于当前托管组件：' + relative)
    return _bounded_path(root, relative), hashes.get(key)


def build_install_plan(catalog, modes):
    """Union of mode closures. Unimplemented rows are not installable."""
    if not isinstance(modes, (list, tuple)) or not modes:
        raise ValueError("安装计划至少要有一个模式")
    by_id = {item["id"]: item for item in catalog["components"]}
    selected = []
    for mode in modes:
        component_id = _MODE_IDS.get(mode, mode)
        if component_id not in by_id:
            raise ValueError("未知模式：" + str(mode))
        selected.append(component_id)
    included = []
    for component_id in selected:
        for dependency in dependency_closure(catalog, component_id):
            if dependency not in included:
                included.append(dependency)
        if component_id not in included:
            included.append(component_id)
    installable = []
    blocked = []
    for component_id in included:
        item = by_id[component_id]
        row = {
            "id": component_id,
            "implemented": bool(item["implemented"]),
            "version": item.get("version"),
        }
        if item["implemented"]:
            installable.append(row)
        else:
            blocked.append(row)
    return {
        "modes": list(modes),
        "component_ids": included,
        "installable": installable,
        "not_installable": blocked,
        "urls": [],
        "conflicts": [],
    }


def installable_ids(catalog, modes):
    plan = build_install_plan(catalog, modes)
    return [row["id"] for row in plan["installable"]]


def marker_path(data_root):
    """Versioned first-run file. It is not the job database."""
    return os.path.join(os.fspath(data_root), "state", MARKER_NAME)


def read_first_run(path):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {"complete": False, "reason": "标记缺失或损坏"}
    if not isinstance(payload, dict) or payload.get("marker_version") != MARKER_VERSION or payload.get("complete") is not True:
        return {"complete": False, "reason": "损坏标记不能当成已完成"}
    return {"complete": True, "reason": "已完成首次配置", "marker_version": MARKER_VERSION}


def write_first_run(path):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    payload = {"marker_version": MARKER_VERSION, "complete": True}
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
    return read_first_run(path)


def choose_install_location(program_root, environ=None, release_mode=False):
    """Reuse the release data-root rule. Development paths stay in the tree."""
    paths = application_data_paths(program_root, environ=environ, release_mode=release_mode)
    return {
        "release_mode": bool(release_mode),
        "program_root": os.path.abspath(os.fspath(program_root)),
        "data_root": str(paths["data_root"]),
        "database": str(paths["database"]),
    }


def measure_bytes(root):
    """Sum real file sizes. The result is bytes, not a hardcoded gigabyte figure."""
    total = 0
    files = []
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            total += size
            files.append({
                "path": os.path.relpath(path, root).replace("\\", "/"),
                "bytes": size,
            })
    files.sort(key=lambda item: item["path"])
    return {"bytes": total, "files": files, "unit": "bytes"}


def transfer_local(source, destination, expected_sha256, retries=_RETRY_CAP, interrupt_after=None,
                   free_bytes=None, required_bytes=None, mirrors=None):
    """Copy a local file with a bounded resume. Failure leaves destination bytes."""
    if type(retries) is not int or isinstance(retries, bool) or retries < 0 or retries > _RETRY_CAP:
        raise ValueError("下载重试超过上限")
    sources = [source] if not mirrors else list(mirrors)
    if not sources:
        raise ValueError("没有本地文件源")
    _plain_path(destination)
    _plain_path(destination + '.part')
    for origin in sources:
        _plain_path(origin)
    before = _snapshot(destination)
    if free_bytes is not None and required_bytes is not None and free_bytes < required_bytes:
        raise OSError("磁盘空间不足")
    part = destination + ".part"
    parent = os.path.dirname(os.path.abspath(destination))
    if parent:
        os.makedirs(parent, exist_ok=True)
    last_error = None
    attempts = 0
    for origin in sources:
        for _try in range(retries + 1):
            attempts += 1
            try:
                _copy_resume(origin, part, interrupt_after)
                if interrupt_after is not None and os.path.isfile(part) and os.path.getsize(part) < os.path.getsize(origin):
                    return {"complete": False, "attempts": attempts, "part": part, "destination_unchanged": _snapshot(destination) == before}
                digest = sha256_file(part)
                if digest != expected_sha256:
                    raise ValueError("校验和不匹配")
                os.replace(part, destination)
                return {"complete": True, "attempts": attempts, "sha256": digest, "destination": destination}
            except (OSError, ValueError) as exc:
                last_error = exc
                if os.path.isfile(part) and isinstance(exc, ValueError):
                    os.remove(part)
                continue
    if _snapshot(destination) != before:
        raise RuntimeError("失败的传输改动了正式文件")
    raise last_error if last_error else OSError("本地传输失败")


def stage_install(source, live_path, staging_dir, expected_sha256, commit=True):
    """Verify in a staging directory. commit False leaves the live file and a token."""
    source, live_path, staging_dir = map(os.fspath, (source, live_path, staging_dir))
    _plain_path(source)
    _plain_path(live_path)
    _plain_path(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)
    token = os.path.join(staging_dir, "INSTALLING.json")
    staged = os.path.join(staging_dir, os.path.basename(live_path))
    _plain_path(token)
    _plain_path(staged)
    before = _snapshot(live_path)
    with open(token, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"live": live_path, "commit": bool(commit)}, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    try:
        result = transfer_local(source, staged, expected_sha256, retries=0)
    except (OSError, ValueError):
        if _snapshot(live_path) != before:
            raise RuntimeError("校验失败时正式文件被改动")
        return {"committed": False, "token": token, "live_unchanged": True}
    if not commit:
        return {"committed": False, "token": token, "staged": result["destination"], "live_unchanged": _snapshot(live_path) == before}
    _plain_path(live_path)
    parent = os.path.dirname(os.path.abspath(live_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    os.replace(staged, live_path)
    os.remove(token)
    return {"committed": True, "live": live_path, "sha256": sha256_file(live_path)}


def incomplete_install(staging_dir):
    token = os.path.join(staging_dir, "INSTALLING.json")
    return os.path.isfile(token)


def repair_managed(package_root, live_root, relative_paths, log_path, in_use=False):
    """Copy listed managed files. Paths outside the list are not touched."""
    lines = []
    if in_use:
        raise RuntimeError('运行中的任务不会切换组件文件')
    targets = _managed_targets(live_root)
    operations = []
    for relative in relative_paths:
        target, expected = _authorize_managed(live_root, relative, targets)
        source = _bounded_path(package_root, relative)
        if not os.path.isfile(source):
            raise FileNotFoundError("修复包缺少 " + relative)
        if expected is None:
            raise ValueError('修复必须有当前文件的冻结SHA-256：' + relative)
        if sha256_file(source) != expected:
            raise ValueError('修复包与当前版本哈希不符：' + relative)
        operations.append((relative, source, target, expected))
    _plain_path(log_path)
    for relative, source, target, expected in operations:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temporary = target + "." + uuid.uuid4().hex + ".tmp"
        try:
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != expected:
                raise ValueError('复制期间修复文件变化：' + relative)
            _plain_path(target)
            os.replace(temporary, target)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)
        lines.append(relative + " " + sha256_file(target))
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(log_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return {"files": list(relative_paths), "log": log_path}


def read_offline_package(folder):
    manifest_path = _bounded_path(folder, 'manifest.json')
    if not os.path.isfile(manifest_path):
        manifest_path = _bounded_path(folder, 'release-manifest.json')
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError('离线清单必须是对象')
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("离线包缺少文件清单")
    seen = set()
    for item in files:
        if not isinstance(item, dict) or "path" not in item or "sha256" not in item:
            raise ValueError("离线包清单不完整")
        payload = _bounded_path(folder, item['path'])
        key = item['path'].replace('\\', '/').casefold()
        if key in seen or not isinstance(item['sha256'], str) or len(item['sha256']) != 64 or any(c not in '0123456789abcdef' for c in item['sha256']):
            raise ValueError('离线清单路径重复或SHA-256无效')
        seen.add(key)
        if sha256_file(payload) != item["sha256"]:
            raise ValueError("离线包校验失败：" + item["path"])
    if manifest.get("unimplemented"):
        raise ValueError("离线包不能声明未实现组件")
    manifest['package_kind'] = 'portable' if os.path.basename(manifest_path) == 'release-manifest.json' else 'components'
    return manifest


def install_offline_package(folder, live_root, in_use=False, *, trusted_public_keys=None):
    if in_use:
        raise RuntimeError('运行中的任务不会切换组件文件')
    with _component_install_lock(live_root):
        return _install_offline_package(folder,live_root,in_use=in_use,trusted_public_keys=trusted_public_keys)


def _install_offline_package(folder, live_root, in_use=False, *, trusted_public_keys=None):
    if in_use:
        raise RuntimeError('运行中的任务不会切换组件文件')
    component_receipt = None
    extracted = None
    temp_root = application_data_paths(live_root)['temp']
    package_path = Path(folder)
    if package_path.suffix.casefold() == '.ampkg':
        from core.component_package import extract_component_package
        extracted = extract_component_package(package_path, current_root=live_root,
                                              trusted_public_keys=trusted_public_keys,
                                              user_temp_root=temp_root)
        folder = extracted['folder']
        component_receipt = extracted['receipt_path']
    result = None
    try:
        result = _install_offline_directory(folder,live_root,component_receipt)
        return result
    finally:
        if extracted is not None:
            try:
                target = _plain_path(Path(extracted['folder']))
                parent = _plain_path(Path(temp_root))
                if target.parent != parent or not target.name.startswith('ampkg-'):
                    raise ValueError('拒绝清理不属于本次操作的组件包目录')
                shutil.rmtree(target)
                if result is not None:result['extracted_payload_retained'] = False
            except Exception as cleanup_error:
                # Preserve the primary transaction error, or truthfully report
                # retained temp data after a successful commit. Never undo a
                # committed repair merely because temp cleanup was denied.
                if result is not None:
                    result['extracted_payload_retained'] = True
                    result['cleanup_error'] = str(cleanup_error)


def _install_offline_directory(folder,live_root,component_receipt=None):
    manifest = read_offline_package(folder)
    targets = _managed_targets(live_root)
    operations = []
    matched = 0
    for item in manifest['files']:
        if manifest['package_kind'] == 'portable':
            key = item['path'].replace('\\', '/').casefold()
            # A complete donor contains documentation and its own manifests.
            # They are verified on read but never applied to the active release.
            if key not in targets[0]:
                continue
        live, expected = _authorize_managed(live_root, item['path'], targets)
        matched += 1
        if expected is not None and os.path.isfile(live) and sha256_file(live) == expected:
            continue
        if expected is not None and expected != item['sha256']:
            raise ValueError('离线包与当前冻结组件哈希不一致：' + item['path'])
        operations.append((item, live))
    if not matched:
        raise ValueError('离线包没有与当前托管组件匹配的文件')
    staging = str(application_data_paths(live_root)['temp'] / ('offline-install-'+uuid.uuid4().hex))
    _plain_path(staging)
    if component_receipt is not None:
        Path(staging).mkdir(parents=True,exist_ok=False)
        target = Path(staging)/'component-package-receipt.json'
        shutil.copyfile(component_receipt,target)
        if sha256_file(target)!=sha256_file(component_receipt):
            raise ValueError('组件包收据保存失败，未提交任何托管文件')
        component_receipt = str(target)
    prepared=[];installed=[]
    try:
        # Verify every staged payload and preserve every previous file before
        # replacing any live file. Each transaction owns a unique directory.
        for index,(item,live) in enumerate(operations):
            before=_snapshot(live)
            source=_bounded_path(folder,item['path'])
            result=stage_install(source,live,os.path.join(staging,str(index)),item['sha256'],commit=False)
            if not result.get('staged'):raise ValueError('离线组件暂存失败：'+item['path'])
            backup=os.path.join(staging,str(index),'previous') if before is not None else None
            if backup is not None:
                shutil.copyfile(live,backup)
                if _snapshot(backup)!=before:raise ValueError('备份期间托管文件发生变化')
            prepared.append((item,live,before,backup,result['staged']))
        for item,live,before,backup,staged in prepared:
            _plain_path(live)
            if _snapshot(live)!=before:raise ValueError('提交期间托管文件发生变化')
            if sha256_file(staged)!=item['sha256']:raise ValueError('暂存组件校验失败')
            os.makedirs(os.path.dirname(os.path.abspath(live)),exist_ok=True)
            os.replace(staged,live);installed.append(live)
            if sha256_file(live)!=item['sha256']:raise ValueError('提交后组件校验失败')
    except Exception:
        try:
            for item,live,before,backup,staged in reversed(prepared):
                if live not in installed:continue
                _plain_path(live)
                if backup is None:os.remove(live)
                else:os.replace(backup,live)
                if _snapshot(live)!=before:raise OSError('回滚后文件校验失败')
        except Exception as error:
            raise RuntimeError('离线组件回滚失败；前版备份保留在 '+staging) from error
        raise
    return {"installed": installed, "network": False, 'status': 'INSTALLED' if installed else 'NO_CHANGE',
            'restart_required':bool(installed),'previous_directory':staging if installed else None,
            'component_package_receipt':component_receipt}


def update_with_rollback(live_path, new_source, expected_sha256, backup_dir, in_use=False):
    """Keep the previous bytes when the new file does not match."""
    if in_use:
        return {"rolled_back": False, "live_unchanged": True, "refused": "运行中的任务不会切换正在使用的文件"}
    _plain_path(live_path)
    _plain_path(new_source)
    _plain_path(backup_dir)
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, os.path.basename(live_path) + "." + uuid.uuid4().hex + ".previous")
    if os.path.isfile(live_path):
        shutil.copyfile(live_path, backup)
    before = _snapshot(live_path)
    staging = os.path.join(backup_dir, "staging")
    result = stage_install(new_source, live_path, staging, expected_sha256, commit=True)
    if not result["committed"]:
        return {"rolled_back": True, "live_unchanged": _snapshot(live_path) == before,
                "live_sha256": sha256_file(live_path) if os.path.isfile(live_path) else None}
    if sha256_file(live_path) != expected_sha256:
        if os.path.isfile(backup):
            shutil.copyfile(backup, live_path)
        elif before is None:
            if os.path.isfile(live_path):
                os.remove(live_path)
        return {"rolled_back": True, "live_sha256": sha256_file(live_path) if os.path.isfile(live_path) else None}
    return {"rolled_back": False, "live_sha256": sha256_file(live_path), "previous": backup if os.path.isfile(backup) else None}


def component_center_rows(registry):
    rows = []
    for component_id in registry["component_order"]:
        item = registry["components"][component_id]
        if not item["implemented"]:
            label = "不可用"
        elif item["status"] == "PRESENT":
            label = "可用"
        elif item["status"] == "MISSING":
            label = "缺失"
        else:
            label = item["status"]
        rows.append({
            "id": component_id,
            "label": label,
            "status": item["status"],
            "implemented": item["implemented"],
        })
    return rows


def plan_model_import(filename, user_root, managed_dir, managed_names):
    """User imports land under the user root and do not replace managed weights."""
    if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError("模型文件名无效")
    if any(character in filename for character in '<>:"|?*\x00') or filename.endswith((' ', '.')):
        raise ValueError('模型文件名含Windows非法字符或ADS路径')
    stem = filename.split('.')[0].upper()
    if stem in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('模型文件名是Windows保留设备名称')
    managed_target = os.path.abspath(os.path.join(os.fspath(managed_dir), filename))
    destination = os.path.abspath(os.path.join(os.fspath(user_root), "imported", filename))
    _plain_path(managed_target)
    _plain_path(destination)
    if os.path.normcase(destination) == os.path.normcase(managed_target):
        raise ValueError("不能覆盖托管权重")
    return {"ownership": "user", "destination": destination, "managed_unchanged": managed_target}


def import_user_model(source, filename, user_root, managed_dir, managed_names):
    extension = Path(filename).suffix.lower()
    if extension not in ('.pth', '.index'):
        raise ValueError('用户模型仅支持安全RVC .pth和Flat/IVFFlat .index')
    plan = plan_model_import(filename, user_root, managed_dir, managed_names)
    _plain_path(source)
    before = _snapshot(plan["managed_unchanged"])
    parent = os.path.dirname(plan["destination"])
    os.makedirs(parent, exist_ok=True)
    temporary = plan["destination"] + "." + uuid.uuid4().hex + ".tmp"
    try:
        expected = sha256_file(source)
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != expected:
            raise ValueError('复制期间用户模型变化')
        _validate_user_model(temporary, extension)
        _plain_path(plan['destination'])
        os.replace(temporary, plan["destination"])
    finally:
        if os.path.isfile(temporary):
            os.remove(temporary)
    if _snapshot(plan["managed_unchanged"]) != before:
        raise RuntimeError("导入改动了托管权重")
    plan["imported"] = plan["destination"]
    return plan


def _validate_user_model(path, extension):
    if extension == '.pth':
        from core.safe_checkpoint import load_rvc_checkpoint, validate_rvc_inference_checkpoint
        checkpoint = load_rvc_checkpoint(path)
        validate_rvc_inference_checkpoint(checkpoint, complete=True)
    else:
        from core.safe_faiss_index import load_faiss_index
        load_faiss_index(path)


def bootstrap_decision(pythonw_exists, release_mode, runtime_verified):
    """Do not launch the main program when the runtime gate has not passed.

    The C# launcher still performs its own verify. This decision does not
    weaken that gate. Native installer/repair rendering belongs to the launcher.
    """
    if not pythonw_exists:
        return {"action": "installer", "launch_main": False, "status": "BOOTSTRAP_REQUIRED"}
    if release_mode and runtime_verified is not True:
        return {"action": "repair", "launch_main": False, "status": "BLOCKED"}
    return {"action": "launch", "launch_main": True, "status": "READY"}


def _copy_resume(origin, part, interrupt_after):
    copied = os.path.getsize(part) if os.path.isfile(part) else 0
    total = os.path.getsize(origin)
    if copied:
        matching = True
        with open(origin, 'rb') as source, open(part, 'rb') as previous:
            remaining = copied
            while remaining:
                amount = min(1024 * 1024, remaining)
                if source.read(amount) != previous.read(amount):
                    matching = False
                    break
                remaining -= amount
        if not matching:
            os.remove(part)
            copied = 0
    if copied > total:
        os.remove(part)
        copied = 0
    with open(origin, "rb") as source, open(part, "ab") as target:
        source.seek(copied)
        remaining = None if interrupt_after is None else max(0, interrupt_after - copied)
        while True:
            if remaining is not None and remaining <= 0:
                break
            chunk = source.read(1024 if remaining is None else min(1024, remaining))
            if not chunk:
                break
            target.write(chunk)
            if remaining is not None:
                remaining -= len(chunk)


def _snapshot(path):
    if not os.path.isfile(path):
        return None
    return (os.path.getsize(path), sha256_file(path))


def _reject_escape(relative):
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")) or ":" in relative:
        raise ValueError("修复路径无效")
    parts = relative.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("修复路径越界")
