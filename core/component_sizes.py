"""Measured component footprints, with shared files and unknown sources explicit.

Install bytes count immutable files, excluding rebuildable __pycache__.
Package RECORDs identify whole Python distributions rather than dist-info probes.
No downloads or writes occur here. A missing download source is not zero bytes.
"""
import csv
import json
import os
import stat
from pathlib import Path

_PACKAGES = {'runtime.torch': 'torch', 'runtime.fairseq': 'fairseq',
             'runtime.faiss': 'faiss-cpu', 'runtime.demucs': 'demucs',
             'runtime.basic_pitch': 'basic-pitch', 'engine.basic_pitch': 'basic-pitch'}
_RVC = 'tools/RVC/RVC-beta-v2-0618'
_EXTRA = {
    'app.core': ['core', 'AI_Music_Processor.pyw', 'process_music.py', 'AI-Music-Processor.exe', 'VERSION'],
    'runtime.python': [_RVC + '/runtime'],
    'engine.rvc': [_RVC + '/' + name for name in ('infer_pack', 'vc_infer_pipeline.py', 'my_utils.py', 'config.py')],
    'engine.separator.hp2': [_RVC + '/infer_uvr5.py', _RVC + '/uvr5_pack'],
    'engine.f0.rmvpe': ['core/rmvpe_adapter.py', 'core/vendor/rmvpe.py', 'core/vendor/rmvpe_eager.py'],
    'engine.separator.kim_vocal_2': ['core/mdx_separator.py', 'core/mdx_worker.py'],
    'engine.separator.inst_hq_3': ['core/mdx_separator.py', 'core/mdx_worker.py'],
}


def _plain(path, checked_directories=None):
    for parent in (path, *path.parents):
        if checked_directories is not None and parent in checked_directories:
            continue
        if os.path.lexists(parent):
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Cannot measure a reparse path: ' + str(parent))
            if checked_directories is not None and stat.S_ISDIR(info.st_mode):
                checked_directories.add(parent)


def _files(path, checked_directories=None):
    _plain(path, checked_directories)
    if path.is_file():
        yield path
    elif path.is_dir():
        for directory, children, names in os.walk(path, followlinks=False):
            children[:] = [name for name in children if name != '__pycache__']
            for name in children:
                _plain(Path(directory) / name, checked_directories)
            for name in names:
                item = Path(directory) / name
                _plain(item, checked_directories)
                if not stat.S_ISREG(item.lstat().st_mode):
                    raise ValueError('Cannot measure a special file: ' + str(item))
                yield item


def _distribution_files(root, package, checked_directories=None):
    wanted = package.lower().replace('_', '-').replace('.', '-')
    sites = [root / 'runtime/addons', root / _RVC / 'runtime/Lib/site-packages']
    for site in sites:
        if not site.is_dir():
            continue
        _plain(site, checked_directories)
        for metadata in sorted(site.glob('*.dist-info/METADATA')):
            _plain(metadata, checked_directories)
            name = next((line[6:].strip() for line in metadata.read_text(encoding='utf-8').splitlines()
                         if line.startswith('Name: ')), '')
            if name.lower().replace('_', '-').replace('.', '-') != wanted:
                continue
            record = metadata.parent / 'RECORD'
            _plain(record, checked_directories)
            with record.open(encoding='utf-8', newline='') as handle:
                for row in csv.reader(handle):
                    if not row:
                        continue
                    candidate = Path(os.path.abspath(site / row[0]))
                    # RECORD can reference runtime Scripts; never include user data.
                    runtime_roots = (root / 'runtime', root / _RVC / 'runtime')
                    if not any(parent in candidate.parents for parent in runtime_roots):
                        if not os.path.lexists(candidate):
                            # --target installs can retain absent wheel script
                            # records outside their actual target directory.
                            continue
                        raise ValueError('Package RECORD escapes runtime roots')
                    if '__pycache__' not in candidate.parts:
                        yield from _files(candidate, checked_directories)
            return


def measure_component_sizes(root, catalog, cache_root=None, download_files=None):
    root = Path(os.path.abspath(os.fspath(root)))
    _plain(root)
    checked_directories = set()
    inventory_path = root / 'runtime-manifest/inventory.json'
    inventory_files = None
    if inventory_path.is_file():
        _plain(inventory_path)
        with inventory_path.open(encoding='utf-8') as handle:
            inventory_files = {Path(os.path.abspath(root / row['path']))
                               for row in json.load(handle)['files']}
        if any(root not in item.parents for item in inventory_files):
            raise ValueError('Runtime inventory escapes application root')
    downloads = download_files or {}
    file_sets, measured, errors = {}, {}, {}
    for component in catalog['components']:
        identifier = component['id']
        paths = set()
        problems = []
        if component['ownership'] == 'managed' and component['kind'] != 'mode_pack' and component['implemented']:
            candidates = list(_EXTRA.get(identifier, []))
            for probe in component['probes']:
                relative = probe.get('release_path') if probe['type'] == 'tool' else probe.get('path')
                if relative:
                    candidates.append(relative)
            try:
                for relative in candidates:
                    candidate = Path(os.path.abspath(root / relative))
                    if root not in candidate.parents:
                        raise ValueError('Component path escapes application root')
                    if inventory_files is not None and candidate.is_dir() and identifier != 'app.core':
                        discovered = set()
                        for item in inventory_files:
                            if candidate in item.parents:
                                discovered.update(_files(item, checked_directories))
                    else:
                        discovered = set(_files(candidate, checked_directories))
                    paths.update(discovered)
                if identifier in _PACKAGES:
                    paths.update(_distribution_files(root, _PACKAGES[identifier], checked_directories))
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
        file_sets[identifier] = paths
        errors[identifier] = problems
    for component in catalog['components']:
        identifier = component['id']
        if component['kind'] == 'mode_pack':
            pending, seen = list(component.get('dependencies', [])) + list(component.get('requires_any', [])), set()
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                match = next((row for row in catalog['components'] if row['id'] == current), None)
                if match:
                    pending.extend(match.get('dependencies', []))
                    pending.extend(match.get('requires_any', []))
                file_sets[identifier].update(file_sets.get(current, set()))
                errors[identifier].extend(errors.get(current, []))
    counts = {}
    unique_files = set().union(*file_sets.values()) if file_sets else set()
    sizes = {}
    for path in unique_files:
        try:
            sizes[path] = path.stat().st_size
        except OSError as exc:
            for identifier, paths in file_sets.items():
                if path in paths:
                    errors[identifier].append(str(exc))
            sizes[path] = 0
    # Pack unions do not add another ownership claim to each underlying file.
    for component in catalog['components']:
        if component['kind'] != 'mode_pack':
            for path in file_sets[component['id']]:
                counts[path] = counts.get(path, 0) + 1
    for component in catalog['components']:
        identifier = component['id']
        paths = file_sets[identifier]
        try:
            install = sum(sizes[path] for path in paths) if paths else None
            shared = sum(sizes[path] for path in paths if counts.get(path, 0) > 1)
            cache = None
            if cache_root is not None:
                cache_base = Path(os.path.abspath(os.fspath(cache_root)))
                cache_path = Path(os.path.abspath(cache_base / identifier))
                if cache_base not in cache_path.parents:
                    raise ValueError('Component cache path escapes cache root')
                cache = sum(path.stat().st_size for path in _files(cache_path))
            download = None
            if identifier in downloads:
                download_path = Path(os.path.abspath(os.fspath(downloads[identifier])))
                _plain(download_path)
                if not download_path.is_file():
                    raise FileNotFoundError(str(download_path))
                download = download_path.stat().st_size
        except (OSError, ValueError) as exc:
            errors[identifier].append(str(exc))
            install, shared, cache, download = None, None, None, None
        measured[identifier] = {'install_bytes': None if errors[identifier] else install,
                               'download_bytes': download, 'cache_bytes': cache,
                               'shared_install_bytes': shared, 'file_count': len(paths),
                               'errors': errors[identifier],
                               'download_status': 'MEASURED_LOCAL_FILE' if download is not None else 'SOURCE_NOT_CONFIGURED',
                               'cache_status': 'MEASURED_COMPONENT_DIRECTORY' if cache_root is not None else 'LOCATION_NOT_CONFIGURED',
                               'mode_pack_policy': 'union of installed managed dependencies and installed alternatives; user models excluded',
                               'measurement': 'actual immutable file stats; shared files are counted once within a component'}
    return {'components': measured, 'unit': 'bytes', 'additive': False,
            'shared_policy': 'Do not sum component footprints; runtime.python contains shared distributions.'}
