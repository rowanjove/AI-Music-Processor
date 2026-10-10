#!/usr/bin/env python3
"""Build and locally verify an allowlisted Windows Portable directory ZIP."""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import wave
import zipfile
from pathlib import Path, PurePosixPath


VERSION_FILE = "VERSION"
MARKER = "PORTABLE_RELEASE"
MARKER_CONTENT = "AI Music Processor Portable Release v1"
RVC = Path("tools/RVC/RVC-beta-v2-0618")
DEMUCS = Path("tools/UVR5/models/Demucs_Models/v3_v4_repo")
RELEASE_MANIFEST = "release-manifest.json"
SHA256_LENGTH = 64
_VERSION_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_FULL_TREE_PREFIXES = (
    "LICENSES/",
    "core/",
    "runtime/",
    "tools/RVC/RVC-beta-v2-0618/runtime/",
    "tools/RVC/RVC-beta-v2-0618/configs/",
    "tools/RVC/RVC-beta-v2-0618/infer_pack/",
    "tools/RVC/RVC-beta-v2-0618/uvr5_pack/",
    "tools/UVR5/models/Demucs_Models/v3_v4_repo/",
)
_EXACT_FILES = {
    "AI_Music_Processor.pyw",
    "process_music.py",
    "VERSION",
    "README.md",
    "CHANGELOG.md",
    "THIRD_PARTY_LICENSES.md",
    "scripts/run_portable_acceptance.py",
    "scripts/build_component_package.py",
    "docs/FRESH_MACHINE_ACCEPTANCE.md",
    "runtime-manifest/components.json",
    "tools/UVR5/models/MDX_Net_Models/Kim_Vocal_2.onnx",
    "tools/UVR5/models/MDX_Net_Models/UVR-MDX-NET-Inst_HQ_3.onnx",
    "tools/UVR5/models/MDX_Net_Models/model_data/model_data.json",
    "tools/RVC/RVC-beta-v2-0618/rmvpe.pt",
    "tools/RVC/RVC-beta-v2-0618/config.py",
    "tools/RVC/RVC-beta-v2-0618/infer_uvr5.py",
    "tools/RVC/RVC-beta-v2-0618/MDXNet.py",
    "tools/RVC/RVC-beta-v2-0618/my_utils.py",
    "tools/RVC/RVC-beta-v2-0618/vc_infer_pipeline.py",
    "tools/RVC/RVC-beta-v2-0618/hubert_base.pt",
    "tools/RVC/RVC-beta-v2-0618/LICENSE",
    "tools/RVC/RVC-beta-v2-0618/使用需遵守的协议-LICENSE.txt",
}
_SUFFIX_TREES = (
    ("tools/RVC/RVC-beta-v2-0618/weights/", (".pth",)),
    ("tools/RVC/RVC-beta-v2-0618/logs/", (".index",)),
    ("tools/RVC/RVC-beta-v2-0618/uvr5_weights/", (".pth", ".onnx")),
)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_reparse(path):
    path = Path(path)
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_relative(value):
    text = str(value).replace("\\", "/")
    parsed = PurePosixPath(text)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if (not text or parsed.is_absolute()
            or any(part in ("", ".", "..") or part.endswith((' ', '.'))
                   or part.split('.')[0].upper() in reserved
                   or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
                   for part in text.split("/"))):
        raise ValueError("unsafe package-relative path: " + text)
    return text


def _files(root, reject_pycache=True):
    root = Path(root).resolve(strict=True)
    if _is_reparse(root) or not root.is_dir():
        raise ValueError("package root must be a plain directory: " + str(root))
    result = []
    stack = [root]
    while stack:
        current = stack.pop()
        if _is_reparse(current):
            raise ValueError("reparse point in package tree: " + str(current))
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
        directories = []
        for entry in entries:
            path = Path(entry.path)
            if _is_reparse(path):
                raise ValueError("reparse point in package tree: " + str(path))
            if entry.is_dir(follow_symlinks=False):
                if entry.name == "__pycache__":
                    if reject_pycache:
                        raise ValueError("bytecode cache in Portable package: " + str(path))
                    continue
                directories.append(path)
            elif entry.is_file(follow_symlinks=False):
                result.append(path)
            else:
                raise ValueError("special file in package tree: " + str(path))
        stack.extend(reversed(directories))
    return sorted(result, key=lambda path: (path.relative_to(root).as_posix().casefold(),
                                            path.relative_to(root).as_posix()))


def _copy_file(source_root, stage_root, relative):
    source_root = Path(source_root).resolve(strict=True)
    stage_root = Path(stage_root).resolve(strict=True)
    relative = Path(_safe_relative(relative))
    source = source_root / relative
    if _is_reparse(source) or not source.is_file():
        raise FileNotFoundError("required allowlisted source file is missing or unsafe: " + str(source))
    target = stage_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(target.parent):
        raise ValueError("staging path contains a reparse point: " + str(target.parent))
    shutil.copyfile(source, target)
    if source.stat().st_size != target.stat().st_size or _sha256(source) != _sha256(target):
        raise IOError("copied source file failed hash check: " + relative.as_posix())
    return target


def _copy_tree(source_root, stage_root, relative, suffixes=None):
    source_root = Path(source_root).resolve(strict=True)
    stage_root = Path(stage_root).resolve(strict=True)
    relative = Path(_safe_relative(relative))
    source = source_root / relative
    if _is_reparse(source) or not source.is_dir():
        raise FileNotFoundError("required allowlisted source directory is missing or unsafe: " + str(source))
    copied = []
    for path in _files(source, reject_pycache=False):
        # __pycache__ is rebuildable; sourceless bytecode elsewhere remains.
        rel_inside = path.relative_to(source)
        if "__pycache__" in rel_inside.parts:
            continue
        if suffixes is not None and path.suffix.lower() not in suffixes:
            continue
        relative_file = relative / rel_inside
        copied.append(_copy_file(source_root, stage_root, relative_file))
    if not copied:
        raise FileNotFoundError("allowlisted tree contains no selected files: " + relative.as_posix())
    return copied


def copy_allowlisted_bundle(source_root, stage_root):
    """Copy application, embedded runtime, inference vendors, and model assets only."""
    source_root = Path(source_root).resolve(strict=True)
    stage_root = Path(stage_root)
    stage_root.mkdir(parents=True, exist_ok=False)
    for filename in ("AI_Music_Processor.pyw", "process_music.py", VERSION_FILE,
                     "README.md", "CHANGELOG.md", "THIRD_PARTY_LICENSES.md",
                     "scripts/build_component_package.py",
                     "scripts/run_portable_acceptance.py",
                     "docs/FRESH_MACHINE_ACCEPTANCE.md",
                     "runtime-manifest/components.json"):
        _copy_file(source_root, stage_root, filename)
    for directory in (Path("LICENSES"), Path("core"), Path("runtime"), RVC / "runtime", RVC / "configs",
                      RVC / "infer_pack", RVC / "uvr5_pack", DEMUCS):
        _copy_tree(source_root, stage_root, directory)
    for filename in ("config.py", "infer_uvr5.py", "MDXNet.py", "my_utils.py", "vc_infer_pipeline.py",
                     "hubert_base.pt", "LICENSE", "使用需遵守的协议-LICENSE.txt"):
        _copy_file(source_root, stage_root, RVC / filename)
    _copy_tree(source_root, stage_root, RVC / "weights", suffixes={".pth"})
    _copy_tree(source_root, stage_root, RVC / "logs", suffixes={".index"})
    _copy_tree(source_root, stage_root, RVC / "uvr5_weights", suffixes={".pth", ".onnx"})
    for optional in ("tools/UVR5/models/MDX_Net_Models/Kim_Vocal_2.onnx",
                     "tools/UVR5/models/MDX_Net_Models/UVR-MDX-NET-Inst_HQ_3.onnx",
                     "tools/UVR5/models/MDX_Net_Models/model_data/model_data.json",
                     "tools/RVC/RVC-beta-v2-0618/rmvpe.pt"):
        if (source_root / optional).exists():
            _copy_file(source_root, stage_root, optional)

    # Windows resolves bare commands from the worker CWD before PATH. Preserve
    # the old files in the source workspace and pin the new staging CWD instead.
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        source_tool = stage_root / "runtime" / "bin" / name
        vendor_tool = stage_root / RVC / name
        vendor_tool.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_tool, vendor_tool)
        if _sha256(source_tool) != _sha256(vendor_tool):
            raise IOError("staging vendor CWD tool differs from runtime/bin: " + name)
    marker = stage_root / MARKER
    marker.write_bytes(MARKER_CONTENT.encode("ascii"))
    return stage_root


def _find_csc():
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for relative in (Path("Microsoft.NET/Framework64/v4.0.30319/csc.exe"),
                     Path("Microsoft.NET/Framework/v4.0.30319/csc.exe")):
        candidate = windows / relative
        if candidate.is_file() and not _is_reparse(candidate):
            return candidate
    raise FileNotFoundError(".NET Framework csc.exe was not found")


def compile_launcher(source_root, stage_root):
    compiler = _find_csc()
    source = Path(source_root) / "app" / "Launcher.cs"
    output = Path(stage_root) / "AI-Music-Processor.exe"
    result = subprocess.run([str(compiler), "/nologo", "/target:winexe",
                             "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll", "/define:PORTABLE_BUILD",
                             "/out:" + str(output), str(source)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=90, check=False)
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError("Portable Launcher compile failed: " + result.stdout + result.stderr)
    return {"path": str(compiler), "sha256": _sha256(compiler), "version": _compiler_version(compiler),
            "launcher_source_sha256": _sha256(source)}


def _compiler_version(compiler):
    # Framework csc.exe has no /version switch (it emits an error for one).
    result = subprocess.run([str(compiler), "/help"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=30, check=False)
    output = result.stdout or result.stderr
    for line in output.splitlines():
        if "Visual C# Compiler version" in line:
            return line.strip()[:200]
    raise RuntimeError("could not identify the Framework C# compiler version: " + output[:1000])


def _git_source_state(source_root):
    def git(*args):
        result = subprocess.run(["git", "-C", str(source_root), *args], capture_output=True,
                                text=True, encoding="utf-8", timeout=30, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    head = git("rev-parse", "HEAD")
    status = git("-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all") or ""
    changed_paths = sorted({line[3:] for line in status.splitlines() if len(line) > 3})
    return {"head": head or "UNBORN_OR_UNAVAILABLE", "dirty": bool(status),
            "changed_paths": changed_paths,
            "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest()}


def write_release_manifest(stage_root, source_root, runtime_summary, compiler_info):
    stage_root = Path(stage_root).resolve(strict=True)
    source_root = Path(source_root).resolve(strict=True)
    records = []
    for path in _files(stage_root):
        relative = _safe_relative(path.relative_to(stage_root).as_posix())
        records.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)})
    records.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    if any(item["path"] == RELEASE_MANIFEST for item in records):
        raise ValueError("release manifest must not hash itself")
    version = (source_root / VERSION_FILE).read_text(encoding="utf-8").strip()
    source_suffixes = {".py", ".pyw", ".cs", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".md", ".txt", ".xml"}
    source_records = [item for item in records if Path(item["path"]).suffix.lower() in source_suffixes
                      and not item["path"].startswith("runtime-manifest/")]
    interpreter = Path(sys.executable).resolve(strict=True)
    component_sizes = None
    catalog_file = stage_root / 'runtime-manifest/components.json'
    if catalog_file.is_file():
        from core.component_sizes import measure_component_sizes
        catalog = json.loads(catalog_file.read_text(encoding='utf-8'))
        component_sizes = measure_component_sizes(stage_root, catalog)
        errors = {identifier: row['errors'] for identifier, row in component_sizes['components'].items()
                  if row['errors']}
        if errors:
            raise RuntimeError('Cannot measure packaged component footprints: ' + json.dumps(errors, ensure_ascii=False))
    manifest = {
        "schema_version": 1,
        "profile": "local-acceptance",
        "version": version,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": _git_source_state(source_root),
        "source_file_inventory_sha256": _json_digest(source_records),
        "source_status": "UNKNOWN",
        "license_status": "UNKNOWN",
        "distribution_status": "UNKNOWN; public release is blocked pending rights review",
        "runtime_snapshot_sha256": runtime_summary["snapshot_sha256"],
        "runtime_file_count": runtime_summary["artifacts"]["inventory"]["file_count"],
        "runtime_inventory_sha256": runtime_summary["artifacts"]["inventory"]["sha256"],
        "build_toolchain": {"python": {"version": platform.python_version(),
                                         "executable": str(interpreter),
                                         "sha256": _sha256(interpreter)},
                             "launcher_compiler": compiler_info},
        "file_count": len(records),
        "file_inventory_sha256": _json_digest(records),
        "files": records,
        "component_sizes": component_sizes,
        "component_sizes_sha256": _json_digest(component_sizes) if component_sizes is not None else None,
        "component_size_scope": "actual staged files; per-component download archives and user cache locations are not configured",
    }
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    temporary = stage_root / ("." + RELEASE_MANIFEST + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, stage_root / RELEASE_MANIFEST)
    return manifest


def verify_release_manifest(package_root):
    package_root = Path(package_root).resolve(strict=True)
    path = package_root / RELEASE_MANIFEST
    if _is_reparse(path) or not path.is_file():
        raise FileNotFoundError("release manifest is missing or unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("release manifest schema is invalid")
    records = document.get("files")
    if not isinstance(records, list) or document.get("file_count") != len(records):
        raise ValueError("release manifest file list is invalid")
    expected = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise ValueError("release manifest file record is invalid")
        relative = _safe_relative(record["path"])
        key = relative.casefold()
        size = record["size"]
        digest = record["sha256"]
        if (key in expected or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or not isinstance(digest, str) or len(digest) != SHA256_LENGTH
                or any(character not in "0123456789abcdef" for character in digest)):
            raise ValueError("release manifest contains duplicate or invalid file records")
        expected[key] = record
    if document.get("file_inventory_sha256") != _json_digest(records):
        raise ValueError("release manifest inventory digest is invalid")
    actual_paths = _files(package_root)
    actual = {path.relative_to(package_root).as_posix().casefold(): path for path in actual_paths}
    actual.pop(RELEASE_MANIFEST.casefold(), None)
    if set(actual) != set(expected):
        raise ValueError("package file set differs from release manifest")
    for key, path in actual.items():
        record = expected[key]
        if path.stat().st_size != record["size"] or _sha256(path) != record["sha256"]:
            raise ValueError("package file hash differs from release manifest: " + record["path"])
    return {"status": "PASS", "file_count": len(actual),
            "file_inventory_sha256": document["file_inventory_sha256"]}


def write_zip(package_root, zip_path):
    package_root = Path(package_root).resolve(strict=True)
    zip_path = Path(zip_path).resolve()
    if zip_path.exists() or _is_reparse(zip_path.parent):
        raise FileExistsError("refusing to overwrite ZIP or write through a reparse point: " + str(zip_path))
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    files = _files(package_root)
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for index, path in enumerate(files, 1):
            relative = path.relative_to(package_root).as_posix()
            info = zipfile.ZipInfo(relative)
            info.compress_type = zipfile.ZIP_STORED
            info.file_size = path.stat().st_size
            info.external_attr = 0o100644 << 16
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            if index % 5000 == 0:
                print("ZIP wrote {:,}/{:,} files".format(index, len(files)), flush=True)
    return {"path": str(zip_path), "size": zip_path.stat().st_size, "sha256": _sha256(zip_path)}


def extract_and_verify(zip_path, destination, expected_file_count):
    zip_path = Path(zip_path).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.exists() or _is_reparse(destination.parent):
        raise FileExistsError("refusing to overwrite extraction directory: " + str(destination))
    destination.mkdir(parents=True)
    seen = set()
    with zipfile.ZipFile(zip_path, "r", allowZip64=True) as archive:
        infos = archive.infolist()
        if len(infos) != expected_file_count:
            raise ValueError("ZIP entry count differs from expected file count")
        for index, info in enumerate(infos, 1):
            relative = _safe_relative(info.filename)
            key = relative.casefold()
            if key in seen or info.is_dir():
                raise ValueError("ZIP contains duplicate or unexpected directory entry: " + relative)
            seen.add(key)
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.resolve().relative_to(destination)
            except ValueError as exc:
                raise ValueError("ZIP member escapes extraction directory: " + relative) from exc
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "r") as source, target.open("xb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if size != info.file_size:
                raise IOError("ZIP member size differs after extraction: " + relative)
            if index % 5000 == 0:
                print("Extracted {:,}/{:,} files".format(index, len(infos)), flush=True)
    return verify_release_manifest(destination)


def write_synthetic_wav(path, seconds=12, sample_rate=44100):
    import math
    import struct
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * sample_rate)
    melody_hz = (220.0, 246.94, 261.63, 293.66, 329.63, 293.66, 261.63, 246.94,
                 220.0, 261.63, 293.66, 220.0)
    note_frames = max(1, int(1.0 * sample_rate))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        block = bytearray()
        for index in range(frames):
            seconds_at = index / sample_rate
            note_index = min(len(melody_hz) - 1, index // note_frames)
            note_offset = index % note_frames / sample_rate
            frequency = melody_hz[note_index] * (1 + 0.004 * math.sin(2 * math.pi * 5 * seconds_at))
            envelope = min(1.0, note_offset / 0.06, (note_frames / sample_rate - note_offset) / 0.08)
            voice = sum(math.sin(2 * math.pi * frequency * harmonic * seconds_at) / harmonic
                        for harmonic in range(1, 7)) / 2.45
            accompaniment = 0.10 * math.sin(2 * math.pi * 110 * seconds_at)
            value = int(0.27 * 32767 * envelope * (voice + accompaniment))
            block.extend(struct.pack("<h", value))
        output.writeframes(block)
    return path


def _load_script_module(source_root, relative, name):
    source_root = Path(source_root).resolve(strict=True)
    # These modules are loaded by file path, so Python may have only
    # ``scripts/`` on sys.path when the builder itself is launched.  Their
    # package imports (for example ``core.paths``) still need the project root.
    source_root_text = str(source_root)
    if source_root_text not in sys.path:
        sys.path.insert(0, source_root_text)
    path = Path(source_root) / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load local build module: " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version_text(source_root):
    text = (Path(source_root) / VERSION_FILE).read_text(encoding="utf-8").strip()
    if not text or _VERSION_NAME.fullmatch(text) is None:
        raise ValueError("VERSION 不能作为 Windows 目录名: " + text)
    return text


def layout_paths(dist_root, version, output_name=None):
    """Versioned package, sibling ZIP, and scratch that is not inside the package."""
    version = str(version).strip()
    if _VERSION_NAME.fullmatch(version) is None:
        raise ValueError("VERSION 不能作为 Windows 目录名: " + version)
    folder = output_name or ("AI-Music-Processor-v" + version + "-win-x64")
    if _VERSION_NAME.fullmatch(folder) is None or folder in ('.', '..'):
        raise ValueError("Unsafe build output name: " + folder)
    dist_root = Path(dist_root)
    return {
        "package": dist_root / folder,
        "zip": dist_root / (folder + "-portable.zip"),
        "scratch": dist_root / (folder + "-scratch"),
        "sums": dist_root / ((folder + "-SHA256SUMS.txt") if output_name else "SHA256SUMS.txt"),
        "report": dist_root / ((folder + "-BUILD_REPORT.md") if output_name else "BUILD_REPORT.md"),
        "exe_name": "AI-Music-Processor.exe",
    }


def allowlisted_relative(relative):
    text = str(relative).replace("\\", "/").lstrip("/")
    if text in _EXACT_FILES:
        return True
    if any(text.startswith(prefix) for prefix in _FULL_TREE_PREFIXES):
        return "__pycache__" not in text.split("/")
    for prefix, suffixes in _SUFFIX_TREES:
        if text.startswith(prefix) and PurePosixPath(text).suffix.lower() in suffixes:
            return True
    return False


def _directory_receives_files(relative):
    """True when the allowlist copies this directory or any path inside it."""
    prefix = str(relative).replace("\\", "/").strip("/")
    directory = prefix + "/"
    if any(item.startswith(directory) or item.rstrip("/") == prefix for item in _FULL_TREE_PREFIXES):
        return True
    if any(prefix.startswith(item) for item in _FULL_TREE_PREFIXES):
        return True
    if any(item.startswith(directory) for item in _EXACT_FILES):
        return True
    return any(item.startswith(directory) for item, _suffixes in _SUFFIX_TREES)


def _probe_is_shipped(probe):
    kind = probe.get("type")
    if kind == "tool":
        return allowlisted_relative(probe.get("release_path", ""))
    if kind == "relative_dir":
        return _directory_receives_files(probe.get("path", ""))
    if kind == "relative_file":
        return allowlisted_relative(probe.get("path", ""))
    return False


def classify_catalog(catalog):
    """Say which catalog rows the allowlist embeds. No download URL is added."""
    included = []
    excluded = []
    for component in catalog.get("components") or []:
        row = {
            "id": component.get("id"),
            "version": component.get("version"),
            "required": bool(component.get("required")),
            "implemented": bool(component.get("implemented")),
        }
        if component.get("ownership") == "user":
            row["reason"] = "用户数据，不打进包"
            excluded.append(row)
            continue
        if component.get("ownership") == "external":
            row["reason"] = "外部程序，不打进包"
            excluded.append(row)
            continue
        if not component.get("implemented"):
            row["reason"] = component.get("unavailable_reason") or "未实现"
            excluded.append(row)
            continue
        missing = []
        for probe in component.get("probes") or []:
            if probe.get("role") == "optional":
                continue
            if probe.get("type") == "user_rvc_models" or not _probe_is_shipped(probe):
                missing.append(str(probe.get("path") or probe.get("release_path") or probe.get("type")))
        if missing:
            row["reason"] = "白名单未包含：" + ", ".join(missing)
            excluded.append(row)
            continue
        row["reason"] = "随包提供"
        included.append(row)
    return {"included": included, "excluded": excluded}


def read_discover_log(path):
    if not path:
        return {"status": "NOT_RUN", "ran": "", "exit": ""}
    text = Path(path).read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    ran = next((line for line in lines if line.startswith("Ran ")), "")
    exit_line = next((line for line in reversed(lines) if line.startswith("EXIT:")), "")
    summaries = [line for line in lines if re.fullmatch(r"OK(?: \(skipped=\d+\))?", line)
                 or line.startswith(('FAILED', 'ERROR:'))]
    completed = bool(ran) and bool(summaries) and bool(re.fullmatch(r"OK(?: \(skipped=\d+\))?", summaries[-1]))
    status = "PASS" if completed and exit_line in ("", "EXIT:0") else "FAIL"
    return {"status": status, "ran": ran, "exit": exit_line}


def compatibility_facts(source_root):
    source_root = Path(source_root)
    job = (source_root / "core" / "job_spec.py").read_text(encoding="utf-8")
    cache = (source_root / "core" / "cache_fingerprint.py").read_text(encoding="utf-8")
    profile = json.loads((source_root / "packaging" / "portable_profile.json").read_text(encoding="utf-8"))
    job_tree, cache_tree = ast.parse(job), ast.parse(cache)
    def _is_schema_slice(slice_node):
        if isinstance(slice_node, ast.Constant):
            return slice_node.value == 'schema_version'
        if hasattr(slice_node, 'value') and isinstance(slice_node.value, ast.Constant):
            return slice_node.value.value == 'schema_version'
        return False

    schema = next((node.value.value for node in ast.walk(job_tree)
                   if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                   and any(isinstance(target, ast.Subscript)
                           and _is_schema_slice(target.slice) for target in node.targets)), None)
    stage_contract = None
    for node in ast.walk(cache_tree):
        if isinstance(node, ast.keyword) and node.arg == 'stage_contract_version' and isinstance(node.value, ast.Constant):
            stage_contract = node.value.value
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument, default in zip(node.args.args[-len(node.args.defaults):], node.args.defaults):
                if argument.arg == 'stage_contract_version' and isinstance(default, ast.Constant):
                    stage_contract = default.value
    return {
        "schema_version": schema,
        "stage_contract_version": stage_contract,
        "public_distribution_status": profile.get("public_distribution_status"),
    }


def write_checksums(path, zip_path):
    digest = _sha256(zip_path)
    payload = digest + "  " + Path(zip_path).name + "\n"
    temporary = Path(str(path) + ".tmp")
    temporary.write_bytes(payload.encode("utf-8"))
    os.replace(temporary, path)
    recorded = Path(path).read_text(encoding="utf-8").split()[0]
    if recorded != digest or recorded != _sha256(zip_path):
        raise RuntimeError("SHA256SUMS does not match the ZIP")
    return digest


def write_build_report(path, payload):
    """Write the delivery note next to the ZIP. The note is not inside the ZIP."""
    included = payload["components"]["included"]
    excluded = payload["components"]["excluded"]
    facts = payload["compatibility"]
    discover = payload["discover"]
    verify = payload["verify_status"]
    lines = [
        "# AI Music Processor Windows x64 Portable 构建报告",
        "",
        "- 版本：" + payload["version"],
        "- 构建时间（UTC）：" + payload["built_at_utc"],
        "- 公开分发：" + str(facts.get("public_distribution_status")),
        "- JobSpec schema_version：" + str(facts.get("schema_version")),
        "- stage_contract_version：" + str(facts.get("stage_contract_version")),
        "- 测试：" + discover.get("status", "NOT_RUN") + " " + discover.get("ran", "") + " " + discover.get("exit", ""),
        "- staged runtime verify：" + str(verify),
        "- 验收状态：" + str(payload.get("acceptance_status")),
        "- ZIP 字节：" + str(payload["zip_size"]),
        "- ZIP SHA-256：" + payload["zip_sha256"],
        "- 组件实际解压大小：包内 release-manifest.json 的 component_sizes（共享文件不可逐项求和）。",
        "- 组件大小记录 SHA-256：" + str(payload.get("component_sizes_sha256")),
        "- 每组件下载包与用户缓存目录未配置时记录 null；整包 ZIP 字节在上方独立测量。",
        "",
        "## 路径",
        "",
        "- ZIP：" + payload["zip_path"],
        "- EXE：" + payload["exe_path"],
        "- BUILD_REPORT.md：" + payload["report_path"],
        "",
        "## 包含的组件",
        "",
    ]
    for row in included:
        lines.append("- {id} version={version} required={required}".format(**row))
    lines.extend(["", "## 未包含的组件", ""])
    for row in excluded:
        lines.append("- {id}：{reason}".format(**row))
    lines.extend([
        "",
        "## 已知限制",
        "",
        "- 这是本机 Portable 包。公开分发状态仍是 UNKNOWN_BLOCKED，不是已清权的公开发布。",
        "- 组件清单没有公网下载地址。未包含的可选或未实现组件只显示缺失或不可用，不能从网上下载安装。",
        "- 用户自己的 models/rvc 权重、任务数据库、缓存和源码开发目录不在包内。",
        "- 未在另一台干净机器、无 GPU 或其他 GPU 上验收。",
        "- 没有人工听审记录。",
        "- 双击启动走现有 Launcher 的快速运行时门禁。本报告里的 runtime verify 是打包时的全量校验。",
        "",
    ])
    text = "\n".join(lines)
    if "http://" in text or "https://" in text:
        raise RuntimeError("构建报告不能写入下载地址")
    temporary = Path(str(path) + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, path)
    return str(Path(path).resolve())


def _refuse_unrelated_delivery_notes(sums_path, report_path, zip_name):
    """Keep an existing checksum or report that names a different artifact."""
    for path in (Path(sums_path), Path(report_path)):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if text.strip() and zip_name not in text:
            raise FileExistsError("refusing to replace an unrelated delivery note: " + str(path))


def _publish_delivery(source_root, layout, package_root, zip_path, verify_status,
                      acceptance_status, discover_log):
    catalog = json.loads((Path(source_root) / "runtime-manifest" / "components.json").read_text(encoding="utf-8"))
    release_manifest = json.loads((Path(package_root) / RELEASE_MANIFEST).read_text(encoding='utf-8'))
    _refuse_unrelated_delivery_notes(layout["sums"], layout["report"], Path(zip_path).name)
    digest = write_checksums(layout["sums"], zip_path)
    report_path = str(Path(layout["report"]).resolve())
    payload = {
        "version": version_text(source_root),
        "built_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "components": classify_catalog(catalog),
        "discover": read_discover_log(discover_log),
        "verify_status": verify_status,
        "acceptance_status": acceptance_status,
        "zip_size": Path(zip_path).stat().st_size,
        "zip_sha256": digest,
        "zip_path": str(Path(zip_path).resolve()),
        "exe_path": str((Path(package_root) / "AI-Music-Processor.exe").resolve()),
        "report_path": report_path,
        "compatibility": compatibility_facts(source_root),
        "component_sizes_sha256": release_manifest.get('component_sizes_sha256'),
    }
    written = write_build_report(layout["report"], payload)
    print("Delivery notes: " + written, flush=True)
    return written


def build(output_root, source_root=None, skip_audio=False, discover_log=None, public_release=False):
    source_root = Path(source_root or Path(__file__).resolve().parent.parent).resolve(strict=True)
    if discover_log is not None and read_discover_log(discover_log)['status'] != 'PASS':
        raise RuntimeError('Provided full-regression evidence did not pass; refusing release build')
    # The builder runs as a script, where sys.path[0] is ``scripts/``. Keep
    # local package imports rooted at the explicitly selected source tree,
    # including when --source-root is used from another working directory.
    source_root_text = str(source_root)
    if source_root_text not in sys.path:
        sys.path.insert(0, source_root_text)
    output_root = Path(output_root)
    if not output_root.is_absolute():
        output_root = source_root / output_root
    expected_parent = (source_root / "dist").resolve()
    if not expected_parent.exists():
        expected_parent.mkdir()
    if _is_reparse(expected_parent) or not expected_parent.is_dir():
        raise ValueError("project dist path must be a plain directory")
    output_root = output_root.resolve()
    try:
        output_root.relative_to(expected_parent)
    except ValueError as exc:
        raise ValueError("build output must be a new task directory under project dist/") from exc
    if output_root == expected_parent:
        raise ValueError("build output must be the versioned package directory, not dist/ itself")
    version = version_text(source_root)
    default_folder = "AI-Music-Processor-v" + version + "-win-x64"
    layout = layout_paths(expected_parent, version,
                          output_root.name if output_root.name != default_folder else None)
    package_root = layout["package"].resolve()
    if output_root != package_root:
        raise ValueError("build output must be the versioned package directory: " + str(package_root))
    zip_path = layout["zip"].resolve()
    scratch = layout["scratch"].resolve()
    for path in (package_root, zip_path, scratch):
        if path.exists():
            raise FileExistsError("build output already exists; refusing to overwrite: " + str(path))
    if _is_reparse(package_root.parent):
        raise ValueError("build output parent is a reparse point")
    _refuse_unrelated_delivery_notes(layout["sums"], layout["report"], zip_path.name)
    # The package directory is created by the allowlist copy. Scratch stays outside it.
    scratch.mkdir(parents=True)
    extracted_root = scratch / "extracted"
    data_root = scratch / "acceptance-data"
    synthetic = scratch / "fixtures" / "合成测试音频.wav"
    runner_cwd = scratch / "runner cwd Unicode 空格"
    runner_cwd.mkdir(parents=True)
    zip_ready = False
    acceptance_status = "NOT_RUN"
    verify_status = None
    try:
        print("Source freeze started", flush=True)
        freezer = _load_script_module(source_root, "scripts/freeze_runtime.py", "_amp_freezer")
        source_summary = freezer.build_snapshot(source_root, source_root / "runtime-manifest", timeout=900)
        print("Source freeze PASS: {} files / {} bytes / {} packages / {}".format(
            source_summary["artifacts"]["inventory"]["file_count"],
            sum(item["size"] for item in json.loads((source_root / "runtime-manifest/inventory.json").read_text(encoding="utf-8"))["files"]),
            source_summary["package_count"], source_summary["snapshot_sha256"]), flush=True)

        from core.license_inventory import generate_license_dossier, verify_license_dossier
        if not (source_root / 'LICENSES').exists():
            generate_license_dossier(source_root)
        license_gate = verify_license_dossier(source_root, public_release=public_release)
        if license_gate['status'] == 'FAIL':
            raise RuntimeError('Public release license gate blocked: ' + json.dumps(license_gate))
        print('Local license dossier verified: {} subjects / {} texts; public redistribution remains unapproved'.format(
            license_gate['subjects'], license_gate['license_texts']), flush=True)

        print("Creating allowlisted staging tree", flush=True)
        copy_allowlisted_bundle(source_root, package_root)
        compiler_info = compile_launcher(source_root, package_root)
        stage_freezer = _load_script_module(source_root, "scripts/freeze_runtime.py", "_amp_stage_freezer")
        print("Portable staging freeze started (includes same-hash RVC vendor CWD FFmpeg)", flush=True)
        runtime_summary = stage_freezer.build_snapshot(package_root, package_root / "runtime-manifest", timeout=900)
        print("Staging freeze PASS: {} files / {} packages / {}".format(
            runtime_summary["artifacts"]["inventory"]["file_count"], runtime_summary["package_count"],
            runtime_summary["snapshot_sha256"]), flush=True)

        verifier = _load_script_module(source_root, "core/runtime_verify.py", "_amp_runtime_verifier")
        data_root.mkdir(parents=True)
        print("Strict staged runtime verification started", flush=True)
        strict_data_root = data_root / "strict-verify"
        strict_data_root.mkdir()
        verify_result = verifier.verify_runtime(root=package_root, data_root=strict_data_root,
                                                python_override=package_root / RVC / "runtime/python.exe",
                                                timeout=900,
                                                progress=lambda message: print(message, flush=True))
        verify_status = verify_result.get("status")
        if verify_status not in ("PASS", "PASS_WITH_WARNINGS"):
            raise RuntimeError("staged runtime verifier failed: " + json.dumps(verify_result, ensure_ascii=False))
        print("Staging strict verify {}".format(verify_status), flush=True)

        staged_license_gate = verify_license_dossier(package_root, public_release=public_release)
        if staged_license_gate['status'] == 'FAIL':
            raise RuntimeError('Staged public release license gate blocked')
        manifest = write_release_manifest(package_root, source_root, runtime_summary, compiler_info)
        print("Release file hash manifest PASS: {} files / {}".format(
            manifest["file_count"], manifest["file_inventory_sha256"]), flush=True)
        zip_result = write_zip(package_root, zip_path)
        zip_ready = True
        print("ZIP completed: {} bytes / sha256 {}".format(zip_result["size"], zip_result["sha256"]), flush=True)
        extraction = extract_and_verify(zip_path, extracted_root, manifest["file_count"] + 1)
        print("Extracted package file hashes PASS: {} files".format(extraction["file_count"]), flush=True)

        if not skip_audio:
            result = run_acceptance(source_root, scratch, extracted_root, runner_cwd,
                                    zip_result, source_summary, runtime_summary, verify_result,
                                    timeout=1200, synthetic_path=synthetic)
            acceptance_status = result.get("status")
            result["package"] = str(package_root)
            result["report"] = str(layout["report"].resolve())
            return result
        acceptance_status = "BUILT"
        return {"status": "BUILT", "output_root": str(package_root), "zip": zip_result,
                "release_manifest_file_count": manifest["file_count"], "runtime": verify_result,
                "report": str(layout["report"].resolve())}
    except Exception:
        if zip_ready and acceptance_status == "NOT_RUN":
            acceptance_status = "FAILED"
        raise
    finally:
        if zip_ready:
            _publish_delivery(source_root, layout, package_root, zip_path,
                              verify_status, acceptance_status, discover_log)


def _windows_only_env(data_root):
    data_root = Path(data_root).resolve()
    for relative in ("temp", "cache/torch", "cache/xdg", "cache/huggingface", "cache/numba"):
        (data_root / relative).mkdir(parents=True, exist_ok=True)
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(str(windows / part) for part in ("System32", ""))
    env["AI_MUSIC_PROCESSOR_DATA_ROOT"] = str(data_root)
    env["LOCALAPPDATA"] = str(data_root / "localappdata")
    env["TEMP"] = str(data_root / "temp")
    env["TMP"] = str(data_root / "temp")
    env["TORCH_HOME"] = str(data_root / "cache" / "torch")
    env["XDG_CACHE_HOME"] = str(data_root / "cache" / "xdg")
    env["HF_HOME"] = str(data_root / "cache" / "huggingface")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["NUMBA_CACHE_DIR"] = str(data_root / "cache" / "numba")
    return env


def _run_utf8_capture(command, cwd, env, timeout):
    """Capture strict UTF-8 stdout (JSON) and replacement-safe stderr logs."""
    raw = subprocess.run(command, cwd=str(cwd), env=env, capture_output=True,
                         text=False, timeout=timeout, check=False)
    stdout = raw.stdout.decode("utf-8")
    stderr = raw.stderr.decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(raw.args, raw.returncode, stdout, stderr)


def _is_cli_job_result(value):
    """Return whether a dict has the top-level contract emitted by process_music."""
    if not isinstance(value, dict) or value.get("status") != "DONE":
        return False
    job_id = value.get("job_id")
    output_paths = value.get("output_paths")
    if not isinstance(job_id, str) or not job_id.strip() or not isinstance(output_paths, dict):
        return False
    if not isinstance(value.get("qc"), dict):
        return False
    return all(isinstance(output_paths.get(name), str) and output_paths[name].strip()
               for name in ("master_wav", "reports_dir"))


def _parse_cli_result(output):
    """Find a complete trailing process_music job JSON, never a nested QC object."""
    if not isinstance(output, str):
        return None
    end = len(output.rstrip())
    if end == 0 or output[end - 1] != "}":
        return None
    depth = 0
    in_string = False
    start = None
    index = end - 1
    while index >= 0:
        char = output[index]
        if char == '"':
            slash_index = index - 1
            while slash_index >= 0 and output[slash_index] == "\\":
                slash_index -= 1
            if (index - slash_index - 1) % 2 == 0:
                in_string = not in_string
            index = slash_index
            continue
        if not in_string:
            if char == "}":
                depth += 1
            elif char == "{":
                depth -= 1
                if depth == 0:
                    start = index
                    break
        index -= 1
    if start is None:
        return None
    decoder = json.JSONDecoder()
    try:
        value, parsed_end = decoder.raw_decode(output, start)
    except json.JSONDecodeError:
        return None
    if parsed_end != end or not _is_cli_job_result(value):
        return None
    return value


def _validate_cli_project_result(cli_result, project_record, expected_mode):
    """Match the CLI JSON to its independently written project manifest."""
    if not _is_cli_job_result(cli_result) or not isinstance(project_record, dict):
        return False
    if (project_record.get("job_id") != cli_result.get("job_id") or
            project_record.get("status") != "DONE" or
            project_record.get("mode") != expected_mode):
        return False
    project_paths = project_record.get("output_paths")
    cli_paths = cli_result["output_paths"]
    if not isinstance(project_paths, dict):
        return False
    for name in ("master_wav", "reports_dir"):
        project_path = project_paths.get(name)
        if not isinstance(project_path, str) or not project_path.strip():
            return False
        if Path(project_path).resolve() != Path(cli_paths[name]).resolve():
            return False
    cli_qc = cli_result.get("qc")
    project_qc = project_record.get("qc")
    if not isinstance(project_qc, dict):
        return False
    return cli_qc.get("status") == project_qc.get("status")


def _runtime_warnings_are_expected(runtime_result):
    from core.release_gate import runtime_report_ready
    return runtime_report_ready(runtime_result)


def _acceptance_status(runtime_result, business_modes, polish_qc_status):
    """Fail on unverified flows; WARN is reserved for CPU/N/A or real QC WARN."""
    runtime_status = runtime_result.get("status") if isinstance(runtime_result, dict) else None
    if runtime_status not in ("PASS", "PASS_WITH_WARNINGS"):
        return "FAILED"
    if runtime_status == "PASS_WITH_WARNINGS" and not _runtime_warnings_are_expected(runtime_result):
        return "FAILED"
    if polish_qc_status not in ("PASS", "WARN"):
        return "FAILED"
    qc_statuses = [polish_qc_status]
    for _mode, result in business_modes.items():
        if not isinstance(result, dict) or result.get("status") != "PASS":
            return "FAILED"
        qc_status = result.get("qc_status")
        if qc_status not in ("PASS", "WARN"):
            return "FAILED"
        qc_statuses.append(qc_status)
    if runtime_status == "PASS_WITH_WARNINGS" or "WARN" in qc_statuses:
        return "PASS_WITH_WARNINGS"
    return "PASS"


def _build_exit_code(result):
    return 1 if result.get("status") == "FAILED" else 0


def _run_business_mode(mode, package_root, runner_cwd, data_root, input_path, timeout):
    mode_data = data_root / ("mode-" + mode)
    mode_data.mkdir(parents=True)
    output_root = mode_data / "output"
    env = _windows_only_env(mode_data / "user-data")
    command = [str(package_root / RVC / "runtime/python.exe"), "-B",
               str(package_root / "process_music.py"), str(input_path),
               "--mode", mode, "--formats", "wav", "--sample-rate", "44100",
               "--clean", "light", "--master", "natural", "--f0", "pm",
               "--output", str(output_root)]
    if mode == "voice_conversion":
        command.extend(["--model", "zhoujielun"])
    if mode == "transcribe":
        command.append("--midi")
    result = _run_utf8_capture(command, runner_cwd, env, timeout)
    if result.returncode != 0:
        return {"status": "FAIL", "mode": mode, "returncode": result.returncode,
                "stdout_tail": result.stdout[-5000:], "stderr_tail": result.stderr[-5000:]}
    parsed = _parse_cli_result(result.stdout)
    if not isinstance(parsed, dict) or parsed.get("status") != "DONE":
        return {"status": "FAIL", "mode": mode,
                "reason": "CLI did not emit a DONE job manifest",
                "stdout_tail": result.stdout[-5000:], "stderr_tail": result.stderr[-5000:]}
    output_wav = Path(parsed.get("output_paths", {}).get("master_wav", ""))
    if not output_wav.is_file() or output_wav.stat().st_size <= 0:
        return {"status": "FAIL", "mode": mode, "reason": "master WAV is missing or empty",
                "result": parsed}
    # The job manifest exposes the reports directory through output_paths.
    reports_dir = Path(parsed.get("output_paths", {}).get("reports_dir", ""))
    project_manifest = reports_dir.parent / "project.json"
    if not project_manifest.is_file():
        return {"status": "FAIL", "mode": mode,
                "reason": "project manifest is missing", "project_dir": str(reports_dir.parent)}
    project_record = json.loads(project_manifest.read_text(encoding="utf-8"))
    if not _validate_cli_project_result(parsed, project_record, mode):
        return {"status": "FAIL", "mode": mode,
                "reason": "CLI job and project manifest disagree", "project_manifest": str(project_manifest)}
    qc = project_record.get("qc")
    qc_status = qc.get("status") if isinstance(qc, dict) else None
    if qc_status not in ("PASS", "WARN"):
        return {"status": "FAIL", "mode": mode,
                "reason": "project QC is missing or did not pass", "qc_status": qc_status,
                "project_manifest": str(project_manifest)}
    mode_outputs = {}
    if mode == "voice_conversion":
        converted = Path(project_record.get("output_paths", {}).get("stems_dir", "")) / "03_vocal_rvc_zhoujielun.wav"
        if not converted.is_file() or converted.stat().st_size <= 0:
            return {"status": "FAIL", "mode": mode, "reason": "RVC vocal output is missing"}
        mode_outputs["rvc_vocal"] = {"path": str(converted), "sha256": _sha256(converted)}
    elif mode == "transcribe":
        midi_dir = Path(project_record.get("output_paths", {}).get("midi_dir", ""))
        midi_files = sorted(midi_dir.glob("*.mid"), key=lambda path: path.name.casefold())
        if not midi_files or any(not path.is_file() or path.stat().st_size <= 0 for path in midi_files):
            return {"status": "FAIL", "mode": mode, "reason": "Transcribe MIDI output is missing or empty"}
        mode_outputs["midi"] = [{"path": str(path), "sha256": _sha256(path)} for path in midi_files]
    return {"status": "PASS", "mode": mode, "job_id": parsed.get("job_id"),
            "project_dir": str(reports_dir.parent), "output_wav": str(output_wav),
            "output_wav_sha256": _sha256(output_wav),
            "project_manifest": str(project_manifest),
            "project_manifest_sha256": _sha256(project_manifest),
            "qc_status": qc_status, "qc": qc, "mode_outputs": mode_outputs,
            "midi_dir": parsed.get("output_paths", {}).get("midi_dir"),
            "output_root": str(output_root)}


def run_acceptance(source_root, output_root, package_root, runner_cwd, zip_result,
                   source_summary, runtime_summary, previous_verify, timeout=1200,
                   synthetic_path=None):
    package_root = Path(package_root).resolve(strict=True)
    output_root = Path(output_root).resolve(strict=True)
    data_root = output_root / "acceptance-data"
    env = _windows_only_env(data_root / "portable-user-data")
    env["LOCALAPPDATA"] = str(data_root / "localappdata")
    artifact_manifest = json.loads((package_root / RELEASE_MANIFEST).read_text(encoding="utf-8"))
    acceptance = {"profile": "local-acceptance", "status": "IN_PROGRESS",
                  "system_path": env["PATH"], "clean_machine": "NOT_RUN",
                  "runtime_precheck": previous_verify, "zip": zip_result,
                  "artifact_source_commit": artifact_manifest.get("source", {}).get("head"),
                  "artifact_source_dirty": artifact_manifest.get("source", {}).get("dirty"),
                  "acceptance_harness": _git_source_state(source_root)}

    print("Extracted Portable strict verification started", flush=True)
    verifier = _load_script_module(source_root, "core/runtime_verify.py", "_amp_extracted_verifier")
    extracted_data_root = data_root / "extracted-user-data"
    extracted_data_root.mkdir(parents=True)
    strict_result = verifier.verify_runtime(root=package_root,
                                            data_root=extracted_data_root,
                                            python_override=package_root / RVC / "runtime/python.exe",
                                            timeout=900,
                                            progress=lambda message: print(message, flush=True))
    if strict_result.get("status") not in ("PASS", "PASS_WITH_WARNINGS"):
        raise RuntimeError("extracted runtime verifier failed: " + json.dumps(strict_result, ensure_ascii=False))
    acceptance["extracted_runtime_verify"] = strict_result

    launcher_data = data_root / "launcher-user-data"
    launcher_data.mkdir(parents=True)
    launcher_env = _windows_only_env(launcher_data)
    launcher_env["LOCALAPPDATA"] = str(launcher_data.parent / "localappdata")
    launcher = package_root / "AI-Music-Processor.exe"
    print("Portable EXE smoke started from Unicode/space CWD", flush=True)
    launch = _run_utf8_capture([str(launcher), "--smoke-test"], runner_cwd,
                               launcher_env, timeout)
    if launch.returncode != 0:
        raise RuntimeError("Portable launcher smoke returned {}: {}{}".format(
            launch.returncode, launch.stdout[-4000:], launch.stderr[-4000:]))
    smoke_path = launcher_data / "temp" / "launcher_smoke.json"
    deadline = time.monotonic() + timeout
    while not smoke_path.is_file() and time.monotonic() < deadline:
        time.sleep(1)
    if not smoke_path.is_file():
        raise TimeoutError("Portable GUI smoke did not write its isolated success marker")
    smoke_result = json.loads(smoke_path.read_text(encoding="utf-8"))
    if smoke_result.get("status") != "PASS":
        raise RuntimeError("Portable GUI smoke did not report PASS")
    acceptance["launcher_smoke"] = {"status": "PASS", "cwd": str(runner_cwd),
                                    "marker": str(smoke_path), "result": smoke_result}

    if synthetic_path is None:
        synthetic_path = output_root / "fixtures" / "合成测试音频.wav"
    input_path = write_synthetic_wav(synthetic_path)
    input_hash = _sha256(input_path)
    audio_data = data_root / "audio-user-data"
    audio_output = data_root / "synthetic-polish-output"
    audio_env = _windows_only_env(audio_data)
    audio_env["LOCALAPPDATA"] = str(data_root / "audio-localappdata")
    cli = package_root / RVC / "runtime/python.exe"
    command = [str(cli), "-B", str(package_root / "process_music.py"), str(input_path),
               "--mode", "polish", "--formats", "wav", "--sample-rate", "44100",
               "--output", str(audio_output)]
    print("Synthetic polish/QC run started", flush=True)
    result = _run_utf8_capture(command, runner_cwd, audio_env, timeout)
    if result.returncode != 0:
        raise RuntimeError("synthetic polish failed with {}: {}{}".format(
            result.returncode, result.stdout[-5000:], result.stderr[-5000:]))
    parsed = _parse_cli_result(result.stdout)
    if not isinstance(parsed, dict) or parsed.get("status") != "DONE":
        raise RuntimeError("synthetic polish CLI did not emit a success result: " + result.stdout[-5000:])
    output_wav = Path(parsed.get("output_paths", {}).get("master_wav", ""))
    if not output_wav.is_file():
        raise RuntimeError("synthetic polish result has no output master WAV")
    reports_dir = Path(parsed.get("output_paths", {}).get("reports_dir", ""))
    polish_manifest = reports_dir.parent / "project.json"
    if not polish_manifest.is_file():
        raise RuntimeError("synthetic polish project manifest is missing")
    polish_record = json.loads(polish_manifest.read_text(encoding="utf-8"))
    polish_qc = polish_record.get("qc")
    polish_qc_status = polish_qc.get("status") if isinstance(polish_qc, dict) else None
    if (not _validate_cli_project_result(parsed, polish_record, "polish") or
            polish_qc_status not in ("PASS", "WARN")):
        raise RuntimeError("synthetic polish project or QC did not pass")
    acceptance["synthetic_polish"] = {
        "status": "PASS", "input_sha256": input_hash,
        "output_wav": str(output_wav), "output_sha256": _sha256(output_wav),
        "qc_status": polish_qc_status, "project_manifest": str(polish_manifest),
        "project_manifest_sha256": _sha256(polish_manifest), "qc": polish_qc,
        "result": parsed,
    }
    business_modes = {}
    for mode in ("voice_conversion", "transcribe"):
        print("Synthetic {} run started".format(mode), flush=True)
        try:
            business_modes[mode] = _run_business_mode(
                mode, package_root, runner_cwd, data_root, input_path, timeout)
        except Exception as exc:
            business_modes[mode] = {"status": "FAIL", "mode": mode,
                                    "error": type(exc).__name__ + ": " + str(exc)}
        print("Synthetic {} result: {}".format(mode, business_modes[mode]["status"]), flush=True)
    acceptance["synthetic_business_modes"] = business_modes
    acceptance["status"] = _acceptance_status(
        strict_result, business_modes, polish_qc_status)
    evidence_path = output_root / "evidence" / "portable-acceptance.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(acceptance, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    print("Local acceptance evidence: " + str(evidence_path), flush=True)
    return {"status": acceptance["status"], "output_root": str(output_root),
            "zip": zip_result, "evidence": str(evidence_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True,
                        help="versioned package directory under dist/; the EXE is at its root")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--discover-log", type=Path, default=None,
                        help="UTF-8 unittest log to quote in BUILD_REPORT.md")
    parser.add_argument("--skip-audio", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--public-release", action="store_true", help="Require verified redistribution rights; UNKNOWN blocks the build")
    args = parser.parse_args(argv)
    print("Local-only build; public distribution status remains UNKNOWN/BLOCKED", flush=True)
    result = build(args.output_root, args.source_root, skip_audio=args.skip_audio,
                   discover_log=args.discover_log, public_release=args.public_release)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return _build_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
