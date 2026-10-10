"""Fail-closed, read-only integrity checks for the packaged runtime.

This module intentionally uses only the Python standard library and never
imports ``core.config`` or ``core.storage``. It may trigger dependency/JIT
initialization in a child process; Numba cache output is redirected to the
selected user data root. A separate temporary write probe checks that root.
"""
from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
EXPECTED_FFMPEG_BUILD = "2024-05-20-git-127ded5078-full_build-www.gyan.dev"
RVC_RUNTIME = "tools/RVC/RVC-beta-v2-0618/runtime"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANCEL_EVENT=contextvars.ContextVar('runtime_verification_cancel',default=None)


def _check_cancel():
    event=_CANCEL_EVENT.get()
    if event is not None and event.is_set():
        raise RuntimeVerificationError('runtime verification cancelled')
REQUIRED_MODULES = (
    "basic_pitch", "demucs", "torch", "numpy", "scipy", "librosa", "faiss", "onnxruntime",
    "soundfile", "tkinter", "fairseq", "pyworld", "parselmouth", "torchcrepe", "ffmpeg",
)
REQUIRED_DB_COLUMNS = {
    "jobs": {"id", "source_path", "mode", "parameters_json", "status", "owner_token", "lease_until"},
    "job_stages": {"job_id", "stage_name", "status", "input_hash", "output_json", "output_hashes_json", "last_used_at"},
    "cache_entries": {"cache_key", "stage_name", "input_hash", "output_json", "output_hashes_json"},
    "output_files": {"job_id", "path", "sha256", "kind"},
    "licenses": {"id", "subject_type", "subject_id", "license_name", "commercial_status"},
}


class RuntimeVerificationError(RuntimeError):
    pass


def _canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_reparse(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeVerificationError("manifest contains an invalid relative path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or re.match(r"^[A-Za-z]:", value) or any(part in ("", ".", "..") for part in value.split("/")):
        raise RuntimeVerificationError("manifest path is absolute or traverses directories: " + value)
    return value


def _resolve_record_path(root, value, require_file=True):
    value = _safe_relative(value)
    path = root.joinpath(*PurePosixPath(value).parts)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeVerificationError("manifest path escapes the application root: " + value) from exc
    current = root
    for part in PurePosixPath(value).parts:
        current = current / part
        if _is_reparse(current):
            raise RuntimeVerificationError("reparse point in runtime path: " + value)
    if require_file and (not path.is_file()):
        raise FileNotFoundError("manifest file is missing: " + value)
    return path


def _hash_file(path, deadline):
    _check_cancel()
    if _is_reparse(path):
        raise RuntimeVerificationError("refusing to hash a reparse point: " + str(path))
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeVerificationError("runtime inventory entry is not a regular file: " + str(path))
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            _check_cancel()
            if time.monotonic() > deadline:
                raise TimeoutError("runtime file hashing exceeded its configured timeout")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    after = os.stat(path, follow_symlinks=False)
    before_signature = (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_dev)
    after_signature = (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_dev)
    if before_signature != after_signature:
        raise RuntimeVerificationError("runtime file changed while hashing: " + str(path))
    return before.st_size, digest.hexdigest()


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeVerificationError("cannot read runtime manifest file: " + str(path)) from exc


def _validate_documents(root):
    from core.paths import PORTABLE_RELEASE_CONTENT, PORTABLE_RELEASE_MARKER
    marker = root / PORTABLE_RELEASE_MARKER
    if _is_reparse(marker) or not marker.is_file():
        raise RuntimeVerificationError("Portable release sentinel requires a plain PORTABLE_RELEASE file")
    try:
        marker_content = marker.read_bytes()
    except OSError as exc:
        raise RuntimeVerificationError("cannot read Portable release marker") from exc
    if marker_content != PORTABLE_RELEASE_CONTENT.encode("ascii"):
        raise RuntimeVerificationError("Portable release marker content is invalid")
    manifest_root = root / "runtime-manifest"
    if not manifest_root.is_dir() or _is_reparse(manifest_root):
        raise RuntimeVerificationError("runtime-manifest directory is missing or unsafe")
    summary = _read_json(manifest_root / "runtime.json")
    inventory = _read_json(manifest_root / "inventory.json")
    packages = _read_json(manifest_root / "packages.lock.json")
    vendor = _read_json(manifest_root / "vendor.json")
    models = _read_json(manifest_root / "models.json")
    if (not isinstance(summary, dict) or isinstance(summary.get("schema_version"), bool)
            or summary.get("schema_version") != SCHEMA_VERSION):
        raise RuntimeVerificationError("unsupported runtime manifest schema")
    if summary.get("snapshot_kind") != "current-installed-local-bundle":
        raise RuntimeVerificationError("runtime manifest snapshot_kind is invalid")
    if summary.get("distribution_status") != "UNKNOWN; public distribution is not cleared by this snapshot":
        raise RuntimeVerificationError("runtime manifest must not claim distribution clearance")
    if summary.get("source_status") != "UNKNOWN" or summary.get("license_status") != "UNKNOWN":
        raise RuntimeVerificationError("runtime source/license status must remain UNKNOWN")
    try:
        file_count = summary["package_count"]
        if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 0:
            raise ValueError("invalid package_count")
        digest = summary["snapshot_sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValueError("invalid snapshot digest")
        stable = {key: value for key, value in summary.items()
                  if key not in ("generated_at_utc", "snapshot_sha256")}
        if _canonical_digest(stable) != digest:
            raise ValueError("summary digest mismatch")
    except (KeyError, ValueError) as exc:
        raise RuntimeVerificationError("runtime summary digest or package count is invalid") from exc

    documents = {"inventory": inventory, "packages": packages, "vendor": vendor, "models": models}
    artifacts = summary.get("artifacts")
    expected_paths = {"inventory": "runtime-manifest/inventory.json", "packages": "runtime-manifest/packages.lock.json",
                      "vendor": "runtime-manifest/vendor.json", "models": "runtime-manifest/models.json"}
    # New freezes bind the component catalog in the same canonical digest
    # protocol. Historical four-document snapshots remain verifiable for
    # genuine prior-version update/rollback; unknown descriptors still fail.
    if isinstance(artifacts, dict) and 'components' in artifacts:
        components = _read_json(manifest_root / 'components.json')
        from core.component_platform import validate_catalog
        try:
            validate_catalog(components)
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeVerificationError('runtime component catalog is invalid') from exc
        documents['components'] = components
        expected_paths['components'] = 'runtime-manifest/components.json'
    if not isinstance(artifacts, dict) or set(artifacts) != set(documents):
        raise RuntimeVerificationError("runtime artifact descriptors are incomplete")
    for name, document in documents.items():
        artifact = artifacts.get(name)
        expected_fields = {"path", "sha256", "file_count"} if name == "inventory" else {"path", "sha256"}
        if not isinstance(artifact, dict) or set(artifact) != expected_fields:
            raise RuntimeVerificationError("runtime artifact descriptor is invalid: " + name)
        if artifact.get("path") != expected_paths[name] or artifact.get("sha256") != _canonical_digest(document):
            raise RuntimeVerificationError("runtime artifact digest does not match: " + name)
        if name == "inventory":
            artifact_count = artifact.get("file_count")
            actual_count = len(inventory.get("files", []))
            if isinstance(artifact_count, bool) or not isinstance(artifact_count, int) or artifact_count != actual_count:
                raise RuntimeVerificationError("runtime inventory file count does not match")

    if (not isinstance(inventory, dict) or isinstance(inventory.get("schema_version"), bool)
            or inventory.get("schema_version") != SCHEMA_VERSION):
        raise RuntimeVerificationError("runtime inventory schema is invalid")
    records = inventory.get("files")
    if not isinstance(records, list) or not records:
        raise RuntimeVerificationError("runtime inventory has no file records")
    inventory_map = {}
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeVerificationError("runtime inventory contains a non-object record")
        path = _safe_relative(record.get("path"))
        size = record.get("size")
        digest = record.get("sha256")
        roles = record.get("roles")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeVerificationError("runtime inventory size is invalid: " + path)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise RuntimeVerificationError("runtime inventory SHA-256 is invalid: " + path)
        if not isinstance(roles, list) or not roles or any(not isinstance(role, str) or not role for role in roles):
            raise RuntimeVerificationError("runtime inventory roles are invalid: " + path)
        key = path.casefold()
        if key in inventory_map:
            raise RuntimeVerificationError("runtime inventory contains duplicate Windows paths: " + path)
        inventory_map[key] = record

    if (not isinstance(packages, dict) or isinstance(packages.get("schema_version"), bool)
            or packages.get("schema_version") != SCHEMA_VERSION):
        raise RuntimeVerificationError("package lock schema is invalid")
    distributions = packages.get("distributions")
    runtime_python = packages.get("runtime_python")
    if not isinstance(distributions, list) or not isinstance(runtime_python, dict):
        raise RuntimeVerificationError("package lock is missing runtime Python or distributions")
    if file_count != len(distributions):
        raise RuntimeVerificationError("runtime package_count does not match package lock")
    if packages.get("rebuild_claim") != "snapshot of the current installed bundle; no network pip rebuild was verified":
        raise RuntimeVerificationError("package lock contains an unsupported rebuild claim")
    if not isinstance(packages.get("search_order"), list) or not packages["search_order"]:
        raise RuntimeVerificationError("package search order is missing")
    for value in packages["search_order"]:
        _safe_relative(value)
    if runtime_python.get("search_roots") != packages["search_order"]:
        raise RuntimeVerificationError("runtime Python and package search roots differ")
    python_summary = summary.get("python")
    if not isinstance(python_summary, dict):
        raise RuntimeVerificationError("runtime Python summary is missing")
    for field in ("implementation", "version", "version_full", "machine", "bits", "executable"):
        if runtime_python.get(field) != python_summary.get(field):
            raise RuntimeVerificationError("runtime Python summary differs from lock: " + field)
    for field in ("executable", "prefix", "base_prefix"):
        _safe_relative(runtime_python.get(field))

    package_keys = set()
    for package in distributions:
        if not isinstance(package, dict):
            raise RuntimeVerificationError("package lock contains an invalid distribution")
        for key in ("name", "normalized_name", "version", "site_root", "metadata_root"):
            if not isinstance(package.get(key), str) or not package[key]:
                raise RuntimeVerificationError("package lock distribution is missing " + key)
        _safe_relative(package["site_root"])
        _safe_relative(package["metadata_root"])
        if package["site_root"].casefold() not in {path.casefold() for path in packages["search_order"]}:
            raise RuntimeVerificationError("package site_root is outside its search order")
        identity = (package["site_root"].casefold(), package["metadata_root"].casefold())
        if identity in package_keys:
            raise RuntimeVerificationError("package lock contains duplicate distribution metadata")
        package_keys.add(identity)
        metadata_files = package.get("metadata_files")
        if not isinstance(metadata_files, list) or not metadata_files:
            raise RuntimeVerificationError("package lock metadata file list is empty")
        for item in metadata_files:
            if not isinstance(item, dict):
                raise RuntimeVerificationError("package metadata record is invalid")
            path = _safe_relative(item.get("path"))
            actual = inventory_map.get(path.casefold())
            if actual is None or actual.get("size") != item.get("size") or actual.get("sha256") != item.get("sha256"):
                raise RuntimeVerificationError("package metadata differs from inventory: " + path)

    resolution = packages.get("module_resolution")
    if not isinstance(resolution, dict) or not resolution:
        raise RuntimeVerificationError("critical module resolution records are missing")
    missing_required = sorted(set(REQUIRED_MODULES) - set(resolution))
    if missing_required:
        raise RuntimeVerificationError("runtime manifest omits required imports: " + ", ".join(missing_required))
    for name, module in resolution.items():
        if not isinstance(name, str) or not isinstance(module, dict):
            raise RuntimeVerificationError("module resolution record is malformed")
        if module.get("status") == "FOUND":
            origin = module.get("origin")
            if not isinstance(origin, str):
                raise RuntimeVerificationError("found module has no origin: " + name)
            if origin not in ("built-in", "frozen"):
                _safe_relative(origin)
                if origin.casefold() not in inventory_map:
                    raise RuntimeVerificationError("module origin is absent from inventory: " + origin)
        elif module.get("status") != "NOT_FOUND":
            raise RuntimeVerificationError("module resolution status is invalid: " + name)
    missing_imports = sorted(name for name in REQUIRED_MODULES
                             if resolution[name].get("status") != "FOUND")
    if missing_imports:
        raise RuntimeVerificationError("required runtime imports are unavailable in the frozen bundle: " +
                                       ", ".join(missing_imports))

    for document_name, document, key in (("vendor", vendor, "roots"), ("models", models, "assets")):
        if (not isinstance(document, dict) or isinstance(document.get("schema_version"), bool)
                or document.get("schema_version") != SCHEMA_VERSION or not isinstance(document.get(key), list)):
            raise RuntimeVerificationError(document_name + " manifest shape is invalid")
    _validate_vendor_ffmpeg(inventory_map, vendor)
    return summary, inventory, packages, vendor, models, inventory_map


def _validate_vendor_ffmpeg(inventory_map, vendor):
    """Require CWD-resolved vendor tools to be the same build as runtime/bin."""
    roots = vendor.get("roots", [])
    by_id = {item.get("id"): item for item in roots if isinstance(item, dict)}
    vendor_root = "tools/RVC/RVC-beta-v2-0618"
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        runtime_path = "runtime/bin/" + name
        vendor_path = vendor_root + "/" + name
        runtime_record = inventory_map.get(runtime_path.casefold())
        vendor_record = inventory_map.get(vendor_path.casefold())
        descriptor = by_id.get("rvc-" + name)
        if (runtime_record is None or vendor_record is None or not isinstance(descriptor, dict)
                or descriptor.get("path") != vendor_path or descriptor.get("files") != [vendor_path]):
            raise RuntimeVerificationError("manifest must inventory both runtime and vendor CWD tool: " + name)
        if (runtime_record.get("size"), runtime_record.get("sha256")) != (
                vendor_record.get("size"), vendor_record.get("sha256")):
            raise RuntimeVerificationError("vendor CWD tool does not match runtime/bin: " + name)


def _collect_actual_paths(root, strict_roots, fast=False):
    actual = set()
    for relative in strict_roots:
        _check_cancel()
        relative = _safe_relative(relative)
        target = _resolve_record_path(root, relative, require_file=False)
        if not target.exists():
            raise FileNotFoundError("strict runtime tree is missing: " + relative)
        if target.is_file():
            actual.add(relative.casefold())
            continue
        if not target.is_dir():
            raise RuntimeVerificationError("strict runtime path is not a file or directory: " + relative)
        stack = [target]
        while stack:
            current = stack.pop()
            if _is_reparse(current):
                raise RuntimeVerificationError("reparse point in runtime tree: " + str(current))
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
            for entry in entries:
                _check_cancel()
                child = Path(entry.path)
                if _is_reparse(child):
                    raise RuntimeVerificationError("reparse point in runtime tree: " + str(child))
                if entry.name == "__pycache__":
                    raise RuntimeVerificationError("unmanifested Python bytecode cache in release tree: " + str(child))
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
                elif entry.is_file(follow_symlinks=False):
                    actual.add(child.relative_to(root).as_posix().casefold())
                else:
                    raise RuntimeVerificationError("special file in runtime tree: " + str(child))
    return actual


def _verify_inventory(root, inventory, inventory_map, vendor, deadline, progress, fast=False):
    if fast:
        strict_roots = ["core", "runtime/bin", "AI_Music_Processor.pyw", "process_music.py"]
    else:
        strict_roots = ["runtime", RVC_RUNTIME, "core", "AI_Music_Processor.pyw", "process_music.py"]
        for item in vendor.get("roots", []):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                strict_roots.append(item["path"])
    actual = _collect_actual_paths(root, strict_roots, fast=fast)
    expected_strict = set()
    for key, record in inventory_map.items():
        path = record["path"]
        if any(path.casefold() == prefix.casefold() or path.casefold().startswith(prefix.rstrip("/").casefold() + "/")
               for prefix in strict_roots):
            expected_strict.add(key)
    if actual != expected_strict:
        missing = sorted(expected_strict - actual)[:5]
        extra = sorted(actual - expected_strict)[:5]
        raise RuntimeVerificationError("runtime file set differs from inventory; missing=" + repr(missing) + "; extra=" + repr(extra))

    started = time.monotonic()
    next_report = started + 5
    critical_prefixes = (
        "core/", "app/", "runtime/bin/", "AI_Music_Processor.pyw", "process_music.py",
        "VERSION", "PORTABLE_RELEASE", "release-manifest.json", "runtime-manifest/",
    )
    records = [r for r in inventory["files"] if any(r["path"].startswith(p) for p in critical_prefixes)] if fast else inventory["files"]
    for index, record in enumerate(records, 1):
        if time.monotonic() > deadline:
            raise TimeoutError("runtime inventory hashing exceeded its configured timeout")
        path = _resolve_record_path(root, record["path"])
        size, digest = _hash_file(path, deadline)
        if size != record["size"] or digest != record["sha256"]:
            raise RuntimeVerificationError("runtime file hash differs from manifest: " + record["path"])
        now = time.monotonic()
        if progress and now >= next_report:
            progress("已校验 {}/{} 个运行文件，已用时 {:.0f}s".format(index, len(records), now - started))
            next_report = now + 5
    return {"status": "PASS", "file_count": len(records),
            "bytes": sum(record["size"] for record in records),
            "elapsed_seconds": round(time.monotonic() - started, 2)}


_PYTHON_PROBE = r'''import importlib
import importlib.metadata
import json
import os
import platform
import struct
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
search_roots = [Path(value).resolve() for value in json.loads(sys.argv[2])]
module_names = json.loads(sys.argv[3])
for value in reversed(search_roots):
    sys.path.insert(0, str(value))
def relative(value):
    resolved = Path(value).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return "<external>"
packages = []
for order, distribution in enumerate(importlib.metadata.distributions(path=[str(p) for p in search_roots])):
    name = distribution.metadata.get("Name")
    if not name:
        raise RuntimeError("distribution has no Name metadata")
    packages.append({"normalized_name": name.lower().replace("_", "-").replace(".", "-"),
                     "version": distribution.version, "site_root": relative(distribution.locate_file("")),
                     "search_order": order})
modules = {}
errors = {}
for name in module_names:
    try:
        module = importlib.import_module(name)
        origin = relative(getattr(module, "__file__", "")) if getattr(module, "__file__", None) else None
        modules[name] = {"status": "FOUND", "origin": origin}
    except Exception as exc:
        modules[name] = {"status": "IMPORT_FAILED", "origin": None}
        errors[name] = type(exc).__name__ + ": " + str(exc)
cuda = {"available": False, "device_name": None, "vram_bytes": None, "error": None}
try:
    import torch
    cuda["available"] = bool(torch.cuda.is_available())
    if cuda["available"]:
        cuda["device_name"] = torch.cuda.get_device_name(0)
        cuda["vram_bytes"] = int(torch.cuda.get_device_properties(0).total_memory)
except Exception as exc:
    cuda["error"] = type(exc).__name__ + ": " + str(exc)
print(json.dumps({"implementation": platform.python_implementation(), "version": platform.python_version(),
                  "version_full": sys.version, "machine": platform.machine(), "bits": struct.calcsize("P") * 8,
                  "executable": relative(sys.executable), "packages": packages, "modules": modules,
                  "import_errors": errors, "cuda": cuda}, ensure_ascii=False, sort_keys=True))
'''


def _probe_python(root, packages, timeout, data_root, python_override=None, fast=False):
    runtime_python = packages["runtime_python"]
    executable = Path(python_override) if python_override else root.joinpath(*PurePosixPath(runtime_python["executable"]).parts)
    search_roots = packages["search_order"]
    modules = ["tkinter", "numpy", "soundfile", "ffmpeg"] if fast else list(packages["module_resolution"])
    command = [str(executable), "-B", "-I", "-c", _PYTHON_PROBE, str(root),
               json.dumps([str(root.joinpath(*PurePosixPath(path).parts)) for path in search_roots]),
               json.dumps(modules)]
    try:
        child_env = dict(os.environ)
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["NUMBA_CACHE_DIR"] = str(Path(data_root) / "cache" / "numba")
        if _CANCEL_EVENT.get() is None:
            result = subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout, check=False,
                                    env=child_env)
        else:
            deadline=time.monotonic()+timeout
            proc=subprocess.Popen(command,cwd=str(root),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding='utf-8',errors='replace',env=child_env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                while True:
                    _check_cancel()
                    if time.monotonic()>deadline:raise subprocess.TimeoutExpired(command,timeout)
                    try:
                        out,err=proc.communicate(timeout=.1)
                        result=subprocess.CompletedProcess(command,proc.returncode,out,err)
                        break
                    except subprocess.TimeoutExpired:pass
            finally:
                if proc.poll() is None:
                    if os.name=='nt':
                        killer=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/taskkill.exe'
                        subprocess.run([str(killer),'/PID',str(proc.pid),'/T','/F'],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                    else:proc.kill()
                    proc.communicate(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("bundled Python import/version probe timed out") from exc
    if result.returncode != 0:
        raise RuntimeVerificationError("bundled Python probe failed: " + result.stderr[-3000:])
    try:
        actual = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeVerificationError("bundled Python probe returned invalid JSON") from exc
    expected_python = runtime_python
    for field in ("implementation", "version", "version_full", "machine", "bits"):
        if actual.get(field) != expected_python.get(field):
            raise RuntimeVerificationError("bundled Python " + field + " differs from manifest")
    expected_packages = sorted((package.get("normalized_name"), package.get("version"), package.get("site_root"))
                                for package in packages["distributions"])
    actual_packages = sorted((package.get("normalized_name"), package.get("version"), package.get("site_root"))
                             for package in actual.get("packages", []))
    if actual_packages != expected_packages:
        raise RuntimeVerificationError("installed Python distribution metadata differs from lock")
    target_module_names = set(modules)
    for name, expected in packages["module_resolution"].items():
        if fast and name not in target_module_names:
            continue
        if name in REQUIRED_MODULES or expected.get("status") == "FOUND":
            found = actual.get("modules", {}).get(name, {})
            if found.get("status") != "FOUND" or found.get("origin") != expected.get("origin"):
                reason = actual.get("import_errors", {}).get(name, "origin or import status changed")
                raise RuntimeVerificationError("required module import failed or changed: " + name + ": " + reason)
    return actual


def _verify_ffmpeg(root, inventory_map, runner=subprocess.run):
    results = {}
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        relative = "runtime/bin/" + name
        record = inventory_map.get(relative.casefold())
        if record is None:
            raise RuntimeVerificationError("manifest does not include " + relative)
        path = _resolve_record_path(root, relative)
        try:
            result = runner([str(path), "-version"], cwd=str(root), capture_output=True,
                            timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(name + " version probe timed out") from exc
        if result.returncode != 0:
            raise RuntimeVerificationError(name + " version probe failed")
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        first_line = output.splitlines()[0] if output.splitlines() else ""
        if EXPECTED_FFMPEG_BUILD not in first_line:
            raise RuntimeVerificationError(name + " build differs from accepted FFmpeg: " + first_line)
        results[name] = {"status": "PASS", "path": relative, "version": first_line}
    return results


def _probe_data_writable(data_root):
    target = Path(data_root)
    probe_parent = target
    while not probe_parent.exists() and probe_parent != probe_parent.parent:
        probe_parent = probe_parent.parent
    if not probe_parent.is_dir() or _is_reparse(probe_parent):
        raise RuntimeVerificationError("data root or nearest existing parent is not a plain directory")
    exact_root = target.exists()
    if exact_root and (not target.is_dir() or _is_reparse(target)):
        raise RuntimeVerificationError("data root is not a plain directory")
    directory = target if exact_root else probe_parent
    fd = None
    probe = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=".amp-runtime-verify-", suffix=".tmp", dir=str(directory))
        probe = Path(raw_path)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(b"runtime-write-probe\n")
            handle.flush()
            os.fsync(handle.fileno())
        return {"status": "PASS" if exact_root else "WARN", "path": str(target),
                "probe_directory": str(directory), "exact_data_root_exists": exact_root,
                "message": "data root writable" if exact_root else "data root will be created by the application; writable parent verified"}
    except OSError as exc:
        raise RuntimeVerificationError("data directory is not writable: " + str(directory)) from exc
    finally:
        if fd is not None:
            os.close(fd)
        if probe is not None:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass


def _verify_database(data_root):
    path = Path(data_root) / "output" / "database" / "app.db"
    if not path.exists():
        return {"status": "N/A", "path": str(path), "message": "no release database exists yet"}
    if _is_reparse(path) or not path.is_file():
        raise RuntimeVerificationError("existing database path is not a regular file")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            connection.execute("PRAGMA query_only=ON")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeVerificationError("existing database failed SQLite integrity_check")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = set(REQUIRED_DB_COLUMNS) - tables
            if missing:
                raise RuntimeVerificationError("existing database is missing required tables: " + ", ".join(sorted(missing)))
            for table, required in REQUIRED_DB_COLUMNS.items():
                columns = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
                if not required.issubset(columns):
                    raise RuntimeVerificationError("existing database schema is incomplete: " + table)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeVerificationError("existing database could not be checked read-only") from exc
    return {"status": "PASS", "path": str(path), "mode": "ro", "integrity": "ok",
            "tables": sorted(REQUIRED_DB_COLUMNS)}


def _default_data_root(root=None):
    root_path = Path(root or Path(__file__).resolve().parent.parent)
    root_str = str(root_path)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from core.paths import release_data_root
    return release_data_root()


def verify_runtime(root=None, data_root=None, timeout=900, progress=None,
                   python_override=None, ffmpeg_runner=subprocess.run, fast=False,cancel_event=None):
    token=_CANCEL_EVENT.set(cancel_event)
    try:
        return _verify_runtime_impl(root,data_root,timeout,progress,python_override,ffmpeg_runner,fast)
    finally:
        _CANCEL_EVENT.reset(token)


def _verify_runtime_impl(root=None, data_root=None, timeout=900, progress=None,
                   python_override=None, ffmpeg_runner=subprocess.run, fast=False):
    """Verify immutable runtime files and runtime readiness without app imports."""
    root = Path(root or Path(__file__).resolve().parent.parent).resolve(strict=True)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    try:
        data_root = Path(data_root) if data_root is not None else _default_data_root(root)
    except Exception as exc:
        return {"status": "FAIL", "checks": {}, "root": str(root), "data_root": None,
                "error": "release data root is invalid: " + str(exc)}
    if not data_root.is_absolute():
        data_root = Path(data_root).expanduser()
    if not data_root.is_absolute():
        result = {"status": "FAIL", "checks": {}, "root": str(root), "data_root": str(data_root),
                  "error": "release data root must be an absolute path"}
        return result
    data_root = data_root.resolve()
    try:
        data_root.relative_to(root)
    except ValueError:
        pass
    else:
        return {"status": "FAIL", "checks": {}, "root": str(root), "data_root": str(data_root),
                "error": "release data root must be outside the immutable program tree"}
    deadline = time.monotonic() + float(timeout)
    if not (root/'runtime-manifest/runtime.json').exists() and (root/'LIGHTWEIGHT_RELEASE').is_file():
        if (root/'release-manifest.json').exists():
            return {'status':'FAIL','root':str(root),'data_root':str(data_root),'checks':{},
                    'error':'full release cannot fall back to a modular profile when its runtime manifest is missing'}
        from core.modular_runtime import verify_modular_runtime
        return verify_modular_runtime(root,data_root,timeout,progress,_CANCEL_EVENT.get())
    result = {"status": "FAIL", "checks": {}, "root": str(root), "data_root": str(data_root)}
    try:
        _check_cancel()
        if progress:
            progress("正在读取并校验 runtime manifest…")
        summary, inventory, packages, vendor, models, inventory_map = _validate_documents(root)
        result["snapshot_sha256"] = summary["snapshot_sha256"]
        if progress:
            progress("开始逐文件 SHA-256 检查，共 {:,} 个文件 / {:,.2f} GiB…".format(
                len(inventory["files"]), sum(item["size"] for item in inventory["files"]) / (1024 ** 3)))
        result["checks"]["inventory"] = _verify_inventory(root, inventory, inventory_map, vendor, deadline, progress, fast=fast)
        if progress:
            progress("正在用 bundled Python 校验版本、包清单和实际 imports…")
        python_result = _probe_python(root, packages, max(1, int(deadline - time.monotonic())),
                                      data_root, python_override, fast=fast)
        result["checks"]["python"] = {"status": "PASS", "version": python_result["version"],
                                      "bits": python_result["bits"], "package_count": len(python_result["packages"]),
                                      "modules": python_result["modules"]}
        result["checks"]["ffmpeg"] = _verify_ffmpeg(root, inventory_map, runner=ffmpeg_runner)
        cuda = python_result.get("cuda", {})
        if cuda.get("available"):
            result["checks"]["cuda"] = {"status": "PASS", "available": True,
                                         "device_name": cuda.get("device_name"), "vram_bytes": cuda.get("vram_bytes")}
        else:
            result["checks"]["cuda"] = {"status": "WARN", "available": False,
                                         "message": "CPU execution remains available",
                                         "error": cuda.get("error")}
        result["checks"]["data"] = _probe_data_writable(data_root)
        result["checks"]["database"] = _verify_database(data_root)
        existing = Path(data_root)
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        free_bytes = shutil.disk_usage(existing).free
        result["checks"]["disk"] = {"status": "WARN" if free_bytes < 1024 ** 3 else "PASS",
                                     "free_bytes": free_bytes, "probe_path": str(existing)}
        statuses = [item.get("status") for item in result["checks"].values()]
        result["status"] = "PASS_WITH_WARNINGS" if "WARN" in statuses or "N/A" in statuses else "PASS"
    except Exception as exc:
        result["error"] = str(exc)
        result["status"] = "FAIL"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify the AI Music Processor portable runtime")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--fast", action="store_true", help="Perform fast check on critical assets and file presence")
    args = parser.parse_args(argv)
    result = verify_runtime(root=args.root, data_root=args.data_root, timeout=args.timeout_seconds,
                            progress=lambda message: print(message, file=sys.stderr, flush=True),
                            fast=args.fast)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] in ("PASS", "PASS_WITH_WARNINGS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
