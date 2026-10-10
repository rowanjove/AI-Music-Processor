#!/usr/bin/env python3
"""Freeze a read-only inventory of the currently installed local runtime."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
PYTHON_REL = Path("tools/RVC/RVC-beta-v2-0618/runtime/python.exe")
RVC_REL = Path("tools/RVC/RVC-beta-v2-0618")
DEMUCS_REL = Path("tools/UVR5/models/Demucs_Models/v3_v4_repo")
MDX_REL = Path("tools/UVR5/models/MDX_Net_Models")
REQUIRED_MODEL_FILES = (
    Path("tools/RVC/RVC-beta-v2-0618/hubert_base.pt"),
    Path("tools/RVC/RVC-beta-v2-0618/uvr5_weights/HP2_all_vocals.pth"),
    Path("runtime/addons/basic_pitch/saved_models/icassp_2022/nmp.onnx"),
    RVC_REL / "rmvpe.pt",
    MDX_REL / "Kim_Vocal_2.onnx",
    MDX_REL / "UVR-MDX-NET-Inst_HQ_3.onnx",
)
IMPORTANT_MODULES = (
    "basic_pitch", "demucs", "torch", "numpy", "scipy", "librosa", "faiss", "onnxruntime",
    "soundfile", "tkinter", "fairseq", "pyworld", "parselmouth", "torchcrepe", "ffmpeg",
)
MODEL_SUFFIXES = {".pth", ".onnx", ".th", ".pt", ".index"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


_PYTHON_PROBE = r'''import importlib.metadata
import importlib.util
import hashlib
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

def safe_entry(entry):
    text = str(entry).replace("\\", "/")
    candidate = Path(text)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in text.split("/")):
        raise RuntimeError("unsafe distribution metadata path: " + text)
    return text

packages = []
seen_identity = set()
for order, distribution in enumerate(importlib.metadata.distributions(path=[str(p) for p in search_roots])):
    name = distribution.metadata.get("Name")
    version = distribution.version
    if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version:
        raise RuntimeError("distribution has missing Name or Version metadata")
    location = relative(distribution.locate_file(""))
    files = distribution.files or ()
    metadata_entries = []
    for item in files:
        raw_entry = str(item).replace("\\", "/")
        if raw_entry.endswith((".dist-info/METADATA", ".egg-info/PKG-INFO")):
            metadata_entries.append(safe_entry(raw_entry))
    if len(metadata_entries) != 1:
        raise RuntimeError("expected one package metadata file for " + name + "; found " + repr(metadata_entries))
    metadata_rel = metadata_entries[0]
    metadata_root = metadata_rel.rsplit("/", 1)[0]
    identity = (location.casefold(), metadata_root.casefold())
    if identity in seen_identity:
        raise RuntimeError("duplicate package metadata location: " + metadata_rel)
    seen_identity.add(identity)
    metadata_dir = Path(distribution.locate_file(metadata_entries[0])).parent.resolve()
    metadata_files = []
    for current, dirs, names in os.walk(metadata_dir, followlinks=False):
        dirs.sort(key=str.casefold)
        names.sort(key=str.casefold)
        for name_in_dir in names:
            file_path = Path(current, name_in_dir)
            if file_path.is_symlink():
                raise RuntimeError("reparse package metadata is not allowed: " + str(file_path))
            info_before = os.stat(file_path, follow_symlinks=False)
            if not os.path.isfile(file_path):
                raise RuntimeError("package metadata entry is not a file: " + str(file_path))
            hasher = hashlib.sha256()
            with file_path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            info_after = os.stat(file_path, follow_symlinks=False)
            if (info_before.st_size, info_before.st_mtime_ns, info_before.st_ctime_ns) != (
                    info_after.st_size, info_after.st_mtime_ns, info_after.st_ctime_ns):
                raise RuntimeError("package metadata changed during inspection: " + str(file_path))
            metadata_files.append({
                "path": relative(file_path), "size": info_before.st_size, "sha256": hasher.hexdigest()
            })
    metadata_files.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    packages.append({
        "name": name,
        "normalized_name": name.lower().replace("_", "-").replace(".", "-"),
        "version": version,
        "search_order": order,
        "site_root": location,
        "metadata_root": relative(metadata_dir),
        "metadata_files": metadata_files,
        "requires_dist": list(distribution.metadata.get_all("Requires-Dist") or ()),
    })

modules = {}
for name in module_names:
    spec = importlib.util.find_spec(name)
    if spec is None:
        modules[name] = {"status": "NOT_FOUND", "origin": None, "locations": []}
        continue
    origin = relative(spec.origin) if spec.origin and spec.origin not in ("built-in", "frozen") else spec.origin
    locations = [relative(value) for value in (spec.submodule_search_locations or ())]
    modules[name] = {"status": "FOUND", "origin": origin, "locations": locations}

runtime_prefix = relative(sys.prefix)
runtime_base_prefix = relative(sys.base_prefix)
sys_paths = [relative(value) for value in sys.path if value and Path(value).exists()]
print(json.dumps({
    "implementation": platform.python_implementation(),
    "version": platform.python_version(),
    "version_full": sys.version,
    "machine": platform.machine(),
    "bits": struct.calcsize("P") * 8,
    "executable": relative(sys.executable),
    "prefix": runtime_prefix,
    "base_prefix": runtime_base_prefix,
    "sys_path": sys_paths,
    "search_roots": [relative(value) for value in search_roots],
    "packages": packages,
    "module_resolution": modules,
}, ensure_ascii=False, sort_keys=True))
'''


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest_json(value):
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _relative(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escaped project root: " + str(path)) from exc
    return relative.as_posix()


def _is_reparse(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)
    except FileNotFoundError:
        return False
    except OSError:
        raise


def _stat_signature(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("expected regular file: " + str(path))
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev)


def _hash_stable_file(root, path):
    path = Path(path)
    if _is_reparse(path):
        raise ValueError("reparse point is not allowed in a frozen inventory: " + str(path))
    before = _stat_signature(path)
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    after = _stat_signature(path)
    if before != after:
        raise RuntimeError("file changed while it was being hashed: " + str(path))
    return {"path": _relative(root, path), "size": before[0], "sha256": hasher.hexdigest()}


def _check_safe_relative(value):
    if not isinstance(value, str) or not value:
        raise ValueError("file path must be a non-empty string")
    if "\\" in value or "\x00" in value:
        raise ValueError("file paths must use safe POSIX separators: " + repr(value))
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("absolute or traversing path is not allowed: " + value)
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("drive-qualified path is not allowed: " + value)
    return value


def validate_file_records(records, label="files", require_roles=False):
    if not isinstance(records, list):
        raise ValueError(label + " must be an array")
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(label + " entries must be objects")
        path = _check_safe_relative(record.get("path"))
        key = path.casefold()
        if key in seen:
            raise ValueError(label + " contains duplicate Windows paths: " + path)
        seen.add(key)
        size = record.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(label + " file size must be a non-negative integer: " + path)
        digest = record.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValueError(label + " SHA-256 must be 64 lowercase hex characters: " + path)
        if require_roles:
            roles = record.get("roles")
            if not isinstance(roles, list) or not roles or any(not isinstance(role, str) or not role for role in roles):
                raise ValueError(label + " roles must be a non-empty array of strings: " + path)
            if len({role.casefold() for role in roles}) != len(roles):
                raise ValueError(label + " roles contain duplicates: " + path)
    return seen


def _walk_files(root, target, skip_pycache=True):
    target = Path(target)
    if _is_reparse(target):
        raise ValueError("reparse point root is not allowed: " + str(target))
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        raise FileNotFoundError("required inventory path is missing: " + str(target))
    stack = [target]
    while stack:
        current = stack.pop()
        if _is_reparse(current):
            raise ValueError("reparse point directory is not allowed: " + str(current))
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        directories = []
        for entry in entries:
            child = Path(entry.path)
            if _is_reparse(child):
                raise ValueError("reparse point is not allowed: " + str(child))
            if entry.is_dir(follow_symlinks=False):
                if skip_pycache and entry.name == "__pycache__":
                    continue
                directories.append(child)
            elif entry.is_file(follow_symlinks=False):
                yield child
            else:
                raise ValueError("special files are not allowed in inventory: " + str(child))
        stack.extend(reversed(directories))


def _collect_tree(root, relative_path, role, files_by_path):
    target = Path(root, relative_path)
    discovered = []
    for path in _walk_files(root, target):
        record = _hash_stable_file(root, path)
        key = record["path"].casefold()
        existing = files_by_path.get(key)
        if existing is None:
            record["roles"] = [role]
            files_by_path[key] = record
        else:
            if existing["path"] != record["path"] or existing["sha256"] != record["sha256"] or existing["size"] != record["size"]:
                raise ValueError("conflicting duplicate inventory path: " + record["path"])
            if role not in existing["roles"]:
                existing["roles"].append(role)
        discovered.append(record["path"])
    return sorted(discovered, key=lambda value: (value.casefold(), value))


def _collect_matching(root, relative_path, suffixes, role, files_by_path, required=False):
    target = Path(root, relative_path)
    if not target.exists():
        if required:
            raise FileNotFoundError("required model path is missing: " + str(target))
        return []
    discovered = []
    for path in _walk_files(root, target):
        if path.suffix.lower() not in suffixes:
            continue
        record = _hash_stable_file(root, path)
        key = record["path"].casefold()
        existing = files_by_path.get(key)
        if existing is None:
            record["roles"] = [role]
            files_by_path[key] = record
        else:
            if existing["path"] != record["path"] or existing["sha256"] != record["sha256"] or existing["size"] != record["size"]:
                raise ValueError("conflicting duplicate model path: " + record["path"])
            if role not in existing["roles"]:
                existing["roles"].append(role)
        discovered.append(record)
    if required and not discovered:
        raise FileNotFoundError("required model inventory is empty: " + str(target))
    return sorted(discovered, key=lambda item: (item["path"].casefold(), item["path"]))


def inspect_python(root, executable, module_names=IMPORTANT_MODULES, timeout=120, allow_external=False):
    root = Path(root).resolve()
    executable = Path(executable).resolve(strict=True)
    if _is_reparse(executable) or not executable.is_file():
        raise ValueError("Python runtime executable is not a plain file: " + str(executable))
    try:
        selected_executable = _relative(root, executable)
    except ValueError as exc:
        if not allow_external:
            raise ValueError("selected Python executable must be inside the project root") from exc
        selected_executable = "<external-fixture-python>"
    addon_root = root / "runtime" / "addons"
    site_root = root / RVC_REL / "runtime" / "lib" / "site-packages"
    search_roots = [addon_root, site_root]
    for path in search_roots:
        if not path.is_dir() or _is_reparse(path):
            raise FileNotFoundError("required package search root is missing or unsafe: " + str(path))
    command = [str(executable), "-B", "-I", "-c", _PYTHON_PROBE, str(root),
               json.dumps([str(path) for path in search_roots]), json.dumps(list(module_names))]
    try:
        probe_env = dict(os.environ)
        probe_env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, check=False,
                                env=probe_env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("bundled Python metadata probe timed out") from exc
    if result.returncode != 0:
        raise RuntimeError("bundled Python metadata probe failed: " + result.stderr[-4000:])
    try:
        details = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("bundled Python metadata probe returned invalid JSON") from exc
    if not isinstance(details, dict) or not isinstance(details.get("packages"), list):
        raise ValueError("bundled Python metadata probe returned an invalid shape")
    if details.get("implementation") != "CPython" or not isinstance(details.get("version"), str):
        raise ValueError("bundled Python metadata is missing implementation or version")
    if isinstance(details.get("bits"), bool) or not isinstance(details.get("bits"), int):
        raise ValueError("bundled Python architecture metadata has invalid type")
    details["executable"] = selected_executable
    if not allow_external:
        runtime_paths = [details.get("prefix"), details.get("base_prefix")] + details.get("sys_path", [])
        if any(value == "<external>" for value in runtime_paths):
            raise ValueError("bundled Python resolves runtime paths outside the project root")
        for module in details.get("module_resolution", {}).values():
            if module.get("origin") == "<external>" or "<external>" in module.get("locations", []):
                raise ValueError("critical import resolves outside the project root")
    validate_packages(details["packages"])
    return details


def validate_packages(packages):
    if not isinstance(packages, list):
        raise ValueError("packages must be an array")
    seen = set()
    orders = set()
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("package entries must be objects")
        for key in ("name", "normalized_name", "version", "site_root", "metadata_root"):
            if not isinstance(package.get(key), str) or not package[key]:
                raise ValueError("package entry is missing string field " + key)
        _check_safe_relative(package["site_root"])
        _check_safe_relative(package["metadata_root"])
        if not package["metadata_root"].casefold().startswith(package["site_root"].rstrip("/").casefold() + "/"):
            raise ValueError("package metadata_root must be beneath site_root: " + package["metadata_root"])
        expected_name = re.sub(r"[-_.]+", "-", package["name"]).lower()
        if package["normalized_name"] != expected_name:
            raise ValueError("package normalized_name does not match name: " + package["name"])
        order = package.get("search_order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise ValueError("package search_order must be a non-negative integer")
        if order in orders:
            raise ValueError("package search_order values must be unique")
        orders.add(order)
        metadata_files = package.get("metadata_files")
        if not isinstance(metadata_files, list) or not metadata_files:
            raise ValueError("package metadata_files must be a non-empty array")
        validate_file_records(metadata_files, "package.metadata_files")
        prefix = package["metadata_root"].rstrip("/").casefold() + "/"
        if any(not record["path"].casefold().startswith(prefix) for record in metadata_files):
            raise ValueError("package metadata file is outside metadata_root: " + package["metadata_root"])
        identity = package["site_root"].casefold() + "|" + package["metadata_root"].casefold()
        if identity in seen:
            raise ValueError("duplicate package metadata identity: " + identity)
        seen.add(identity)
        requirements = package.get("requires_dist")
        if not isinstance(requirements, list) or any(not isinstance(value, str) for value in requirements):
            raise ValueError("package requires_dist must be an array of strings")


def _collect_model_records(root, files_by_path):
    model_groups = []

    def add_group(identifier, relpath, suffixes, required=False, optional=False):
        entries = _collect_matching(root, relpath, suffixes, "model:" + identifier,
                                    files_by_path, required=required)
        model_groups.append({
            "id": identifier,
            "root": Path(relpath).as_posix(),
            "status": "PRESENT" if entries else ("OPTIONAL_ABSENT" if optional else "EMPTY"),
            "source_status": "UNKNOWN",
            "license_status": "UNKNOWN",
            "distribution_status": "UNKNOWN",
            "files": entries,
        })

    for relpath in REQUIRED_MODEL_FILES:
        path = Path(root, relpath)
        record = _hash_stable_file(root, path)
        record["roles"] = ["model:" + relpath.stem]
        key = record["path"].casefold()
        existing = files_by_path.get(key)
        if existing:
            if existing["sha256"] != record["sha256"] or existing["size"] != record["size"]:
                raise ValueError("model changed during inventory collection: " + record["path"])
            existing["roles"].extend(role for role in record["roles"] if role not in existing["roles"])
        else:
            files_by_path[key] = record
        model_groups.append({
            "id": relpath.stem,
            "root": relpath.as_posix(),
            "status": "PRESENT",
            "source_status": "UNKNOWN",
            "license_status": "UNKNOWN",
            "distribution_status": "UNKNOWN",
            "files": [{key: value for key, value in record.items() if key != "roles"}],
        })

    add_group("rvc-weights", RVC_REL / "weights", {".pth"}, required=True)
    add_group("rvc-indexes", RVC_REL / "logs", {".index"}, required=True)
    add_group("uvr-models", RVC_REL / "uvr5_weights", {".pth", ".onnx"}, required=True)
    add_group("demucs-checkpoints", DEMUCS_REL, {".th"}, required=True)
    add_group("custom-rvc-models", Path("models/rvc"), {".pth", ".index"}, optional=True)
    return {"schema_version": SCHEMA_VERSION, "assets": model_groups}


def _collect_vendor_records(root, files_by_path):
    rvc_root = RVC_REL
    roots = []
    for identifier, relpath in (
        ("rvc-configs", rvc_root / "configs"),
        ("rvc-infer-pack", rvc_root / "infer_pack"),
        ("rvc-uvr5-pack", rvc_root / "uvr5_pack"),
    ):
        files = _collect_tree(root, relpath, "vendor:" + identifier, files_by_path)
        roots.append({"id": identifier, "path": relpath.as_posix(), "files": files})
    top_level_files = (
        "config.py", "infer_uvr5.py", "my_utils.py", "vc_infer_pipeline.py"
    )
    for name in top_level_files:
        relpath = rvc_root / name
        files = _collect_tree(root, relpath, "vendor:rvc-inference", files_by_path)
        roots.append({"id": "rvc-" + name, "path": relpath.as_posix(), "files": files})

    # The workers change CWD to the RVC vendor root before invoking bare
    # ``ffmpeg``. Include these two CWD-resolved tools in the immutable vendor
    # inventory; Portable staging must place copies matching runtime/bin here.
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        relpath = rvc_root / name
        files = _collect_tree(root, relpath, "vendor:rvc-cwd-tool", files_by_path)
        roots.append({"id": "rvc-" + name, "path": relpath.as_posix(), "files": files})

    demucs_root = DEMUCS_REL
    demucs_vendor_files = []
    for path in _walk_files(root, Path(root, demucs_root)):
        if path.suffix.lower() == ".th":
            continue
        record = _hash_stable_file(root, path)
        key = record["path"].casefold()
        existing = files_by_path.get(key)
        if existing is None:
            record["roles"] = ["vendor:demucs-repo"]
            files_by_path[key] = record
        else:
            if existing["sha256"] != record["sha256"] or existing["size"] != record["size"]:
                raise ValueError("vendor file changed during inventory collection: " + record["path"])
            if "vendor:demucs-repo" not in existing["roles"]:
                existing["roles"].append("vendor:demucs-repo")
        demucs_vendor_files.append(record["path"])
    if not demucs_vendor_files:
        raise FileNotFoundError("Demucs inference repository has no vendor/config files")
    roots.append({"id": "demucs-repo", "path": demucs_root.as_posix(),
                  "files": sorted(demucs_vendor_files, key=lambda value: (value.casefold(), value))})
    mdx_metadata = MDX_REL / "model_data/model_data.json"
    files = _collect_tree(root, mdx_metadata, "vendor:mdx-metadata", files_by_path)
    roots.append({"id": "mdx-metadata", "path": mdx_metadata.as_posix(), "files": files})
    return {
        "schema_version": SCHEMA_VERSION,
        "source_status": "UNKNOWN",
        "license_status": "UNKNOWN",
        "distribution_status": "UNKNOWN",
        "roots": roots,
    }


def _atomic_json(path, value):
    path = Path(path)
    parent = path.parent
    existing_parent = parent
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    if _is_reparse(existing_parent):
        raise ValueError("manifest output directory must not be a reparse point: " + str(existing_parent))
    parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(parent):
        raise ValueError("manifest output directory must not be a reparse point: " + str(parent))
    if path.exists() and _is_reparse(path):
        raise ValueError("manifest output file must not be a reparse point: " + str(path))
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def validate_snapshot(summary, inventory, packages, vendor, models, components):
    if not isinstance(summary, dict) or isinstance(summary.get("schema_version"), bool) or summary.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("runtime summary has invalid schema_version")
    summary_fields = ("snapshot_kind", "generated_at_utc", "distribution_status", "source_status",
                      "license_status", "python", "package_count", "artifacts", "snapshot_sha256")
    for key in summary_fields:
        if key not in summary:
            raise ValueError("runtime summary is missing " + key)
    if set(summary) != {"schema_version", *summary_fields}:
        raise ValueError("runtime summary contains unsupported fields")
    if summary["snapshot_kind"] != "current-installed-local-bundle":
        raise ValueError("runtime snapshot_kind is unsupported")
    if summary["distribution_status"] != "UNKNOWN; public distribution is not cleared by this snapshot":
        raise ValueError("distribution_status must keep public distribution uncleared")
    if summary["source_status"] != "UNKNOWN" or summary["license_status"] != "UNKNOWN":
        raise ValueError("source and license status must remain UNKNOWN")
    if not isinstance(summary["generated_at_utc"], str):
        raise ValueError("generated_at_utc must be a string")
    try:
        generated = dt.datetime.fromisoformat(summary["generated_at_utc"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at_utc must be an ISO-8601 timestamp") from exc
    if generated.tzinfo is None or generated.utcoffset() != dt.timedelta(0):
        raise ValueError("generated_at_utc must be UTC")
    digest = summary["snapshot_sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ValueError("snapshot_sha256 must be 64 lowercase hex characters")
    python = summary["python"]
    python_fields = ("implementation", "version", "version_full", "machine", "bits", "executable")
    if not isinstance(python, dict) or set(python) != set(python_fields):
        raise ValueError("runtime Python summary has invalid fields")
    for key in python_fields:
        value = python[key]
        if key == "bits":
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("runtime Python bits must be a positive integer")
        elif not isinstance(value, str) or not value:
            raise ValueError("runtime Python field must be a non-empty string: " + key)
    _check_safe_relative(python["executable"])
    package_count = summary["package_count"]
    if isinstance(package_count, bool) or not isinstance(package_count, int) or package_count < 0:
        raise ValueError("package_count must be a non-negative integer")
    if not isinstance(inventory, dict) or isinstance(inventory.get("schema_version"), bool) or inventory.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("inventory has invalid schema_version")
    if set(inventory) != {"schema_version", "files", "cache_policy"}:
        raise ValueError("inventory has invalid fields")
    inventory_records = inventory.get("files")
    inventory_paths = validate_file_records(inventory_records, "inventory.files", require_roles=True)
    inventory_by_path = {record["path"].casefold(): record for record in inventory_records}
    cache_policy = inventory.get("cache_policy")
    if cache_policy != {"excluded_directory": "__pycache__", "sourceless_bytecode": "included outside __pycache__"}:
        raise ValueError("inventory cache_policy is invalid")
    if not isinstance(packages, dict) or isinstance(packages.get("schema_version"), bool) or packages.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("package lock has invalid schema_version")
    package_fields = {"schema_version", "runtime_python", "search_order", "module_resolution", "distributions", "rebuild_claim"}
    if set(packages) != package_fields:
        raise ValueError("package lock has invalid fields")
    validate_packages(packages.get("distributions"))
    if package_count != len(packages["distributions"]):
        raise ValueError("runtime package_count does not match package lock")
    if packages["rebuild_claim"] != "snapshot of the current installed bundle; no network pip rebuild was verified":
        raise ValueError("package rebuild_claim must describe an installed snapshot")
    runtime_python = packages.get("runtime_python")
    runtime_python_fields = {"implementation", "version", "version_full", "machine", "bits", "executable",
                             "prefix", "base_prefix", "sys_path", "search_roots"}
    if not isinstance(runtime_python, dict) or set(runtime_python) != runtime_python_fields:
        raise ValueError("package lock runtime_python has invalid fields")
    for key in ("implementation", "version", "version_full", "machine", "executable", "prefix", "base_prefix"):
        if not isinstance(runtime_python[key], str) or not runtime_python[key]:
            raise ValueError("package lock runtime_python field must be a non-empty string: " + key)
    for key in ("executable", "prefix", "base_prefix"):
        _check_safe_relative(runtime_python[key])
    if isinstance(runtime_python["bits"], bool) or not isinstance(runtime_python["bits"], int) or runtime_python["bits"] <= 0:
        raise ValueError("package lock runtime_python.bits must be a positive integer")
    search_order = packages.get("search_order")
    if not isinstance(search_order, list) or not search_order:
        raise ValueError("package search_order must be a non-empty array")
    for path in search_order:
        _check_safe_relative(path)
    if len({path.casefold() for path in search_order}) != len(search_order):
        raise ValueError("package search_order contains duplicate roots")
    if runtime_python.get("search_roots") != search_order:
        raise ValueError("runtime Python search roots do not match package search_order")
    sys_path = runtime_python.get("sys_path")
    if not isinstance(sys_path, list):
        raise ValueError("runtime Python sys_path must be an array")
    for path in sys_path:
        _check_safe_relative(path)
    for field in ("implementation", "version", "version_full", "machine", "bits", "executable"):
        if runtime_python[field] != python[field]:
            raise ValueError("runtime Python summary and package lock differ: " + field)
    if not all(item["site_root"].casefold() in {path.casefold() for path in search_order}
               for item in packages["distributions"]):
        raise ValueError("package site_root is not in package search_order")
    package_orders = [item["search_order"] for item in packages["distributions"]]
    if package_orders != sorted(package_orders):
        raise ValueError("package distributions are not in recorded search order")
    for package in packages["distributions"]:
        for record in package["metadata_files"]:
            actual = inventory_by_path.get(record["path"].casefold())
            if actual is None or actual["size"] != record["size"] or actual["sha256"] != record["sha256"]:
                raise ValueError("package metadata file differs from inventory: " + record["path"])
    resolution = packages.get("module_resolution")
    if not isinstance(resolution, dict):
        raise ValueError("module_resolution must be an object")
    for name, module in resolution.items():
        if not isinstance(name, str) or not name or not isinstance(module, dict) or set(module) != {"status", "origin", "locations"}:
            raise ValueError("module_resolution entry has invalid shape")
        status = module["status"]
        if status not in ("FOUND", "NOT_FOUND"):
            raise ValueError("module_resolution status is invalid: " + name)
        origin = module["origin"]
        locations = module["locations"]
        if not isinstance(locations, list) or any(not isinstance(value, str) for value in locations):
            raise ValueError("module_resolution locations must be strings: " + name)
        if status == "NOT_FOUND" and (origin is not None or locations):
            raise ValueError("NOT_FOUND module must not have origin or locations: " + name)
        if status == "FOUND":
            if not isinstance(origin, str) or not origin:
                raise ValueError("FOUND module must have an origin: " + name)
            if origin not in ("built-in", "frozen"):
                _check_safe_relative(origin)
                if origin.casefold() not in inventory_paths:
                    raise ValueError("module origin is absent from inventory: " + origin)
            for value in locations:
                _check_safe_relative(value)
                if not any(path.casefold() == value.casefold() or path.casefold().startswith(value.rstrip("/").casefold() + "/")
                           for path in inventory_paths):
                    raise ValueError("module location is absent from inventory: " + value)
    if not isinstance(vendor, dict) or isinstance(vendor.get("schema_version"), bool) or vendor.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("vendor records have invalid schema_version")
    if set(vendor) != {"schema_version", "source_status", "license_status", "distribution_status", "roots"}:
        raise ValueError("vendor records have invalid fields")
    if any(vendor[key] != "UNKNOWN" for key in ("source_status", "license_status", "distribution_status")):
        raise ValueError("vendor source, license, and distribution status must remain UNKNOWN")
    if not isinstance(vendor.get("roots"), list) or not vendor["roots"]:
        raise ValueError("vendor roots must be an array")
    vendor_ids = set()
    for item in vendor["roots"]:
        if not isinstance(item, dict) or set(item) != {"id", "path", "files"} or not isinstance(item.get("files"), list):
            raise ValueError("vendor root is missing file list")
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("vendor root id must be a non-empty string")
        if item["id"].casefold() in vendor_ids:
            raise ValueError("duplicate vendor root id: " + item["id"])
        vendor_ids.add(item["id"].casefold())
        _check_safe_relative(item.get("path"))
        if not item["files"]:
            raise ValueError("vendor root must include at least one file: " + item["id"])
        root_prefix = item["path"].rstrip("/").casefold() + "/"
        vendor_files = set()
        for path in item["files"]:
            _check_safe_relative(path)
            if path.casefold() in vendor_files:
                raise ValueError("duplicate file in vendor root: " + path)
            vendor_files.add(path.casefold())
            if path.casefold() not in inventory_paths:
                raise ValueError("vendor file is absent from inventory: " + path)
            if path.casefold() != item["path"].casefold() and not path.casefold().startswith(root_prefix):
                raise ValueError("vendor file is outside declared root: " + path)
    if not isinstance(models, dict) or models.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("model records have invalid schema_version")
    if set(models) != {"schema_version", "assets"}:
        raise ValueError("model records have invalid fields")
    if not isinstance(models.get("assets"), list):
        raise ValueError("model assets must be an array")
    model_ids = set()
    for asset in models["assets"]:
        if not isinstance(asset, dict) or set(asset) != {"id", "root", "status", "source_status", "license_status", "distribution_status", "files"} or not isinstance(asset.get("files"), list):
            raise ValueError("model asset is missing file list")
        for key in ("id", "root", "status", "source_status", "license_status", "distribution_status"):
            if not isinstance(asset.get(key), str) or not asset[key]:
                raise ValueError("model asset is missing string field " + key)
        if asset["id"].casefold() in model_ids:
            raise ValueError("duplicate model asset id: " + asset["id"])
        model_ids.add(asset["id"].casefold())
        if asset["status"] not in ("PRESENT", "OPTIONAL_ABSENT", "EMPTY"):
            raise ValueError("model asset status is invalid: " + asset["id"])
        if any(asset[key] != "UNKNOWN" for key in ("source_status", "license_status", "distribution_status")):
            raise ValueError("model source, license, and distribution status must remain UNKNOWN")
        _check_safe_relative(asset["root"])
        paths = validate_file_records(asset["files"], "models." + asset["id"])
        if not paths.issubset(inventory_paths):
            raise ValueError("model file is absent from inventory: " + asset["id"])
        if asset["status"] == "PRESENT" and not asset["files"]:
            raise ValueError("PRESENT model asset has no files: " + asset["id"])
        if asset["status"] != "PRESENT" and asset["files"]:
            raise ValueError("non-present model asset has files: " + asset["id"])
        root_prefix = asset["root"].rstrip("/").casefold() + "/"
        for record in asset["files"]:
            if record["path"].casefold() != asset["root"].casefold() and not record["path"].casefold().startswith(root_prefix):
                raise ValueError("model file is outside declared root: " + record["path"])
            actual = inventory_by_path.get(record["path"].casefold())
            if actual is None or actual["size"] != record["size"] or actual["sha256"] != record["sha256"]:
                raise ValueError("model file differs from inventory: " + record["path"])
    from core.component_platform import validate_catalog
    validate_catalog(components)
    artifacts = summary["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != {"inventory", "packages", "vendor", "models", "components"}:
        raise ValueError("runtime artifacts must be an object")
    expected = {"inventory": inventory, "packages": packages, "vendor": vendor, "models": models,
                "components": components}
    expected_paths = {"inventory": "runtime-manifest/inventory.json", "packages": "runtime-manifest/packages.lock.json",
                      "vendor": "runtime-manifest/vendor.json", "models": "runtime-manifest/models.json",
                      "components": "runtime-manifest/components.json"}
    for name, document in expected.items():
        artifact = artifacts.get(name)
        if not isinstance(artifact, dict):
            raise ValueError("runtime summary is missing artifact " + name)
        expected_keys = {"path", "sha256", "file_count"} if name == "inventory" else {"path", "sha256"}
        if set(artifact) != expected_keys or artifact.get("path") != expected_paths[name]:
            raise ValueError("runtime artifact descriptor is malformed: " + name)
        if artifact.get("sha256") != _digest_json(document):
            raise ValueError("runtime summary digest does not match " + name)
        if not isinstance(artifact.get("sha256"), str) or not SHA256_RE.fullmatch(artifact["sha256"]):
            raise ValueError("runtime artifact SHA-256 is malformed: " + name)
        if name == "inventory" and artifact.get("file_count") != len(inventory_records):
            raise ValueError("runtime inventory file count does not match")
    stable_summary = {key: value for key, value in summary.items()
                      if key not in ("generated_at_utc", "snapshot_sha256")}
    if summary["snapshot_sha256"] != _digest_json(stable_summary):
        raise ValueError("runtime snapshot digest does not match summary")
    if not isinstance(summary["distribution_status"], str):
        raise ValueError("distribution_status must be a string")
    return True


def build_snapshot(root, output_dir, python_executable=None, python_info=None,
                   module_names=IMPORTANT_MODULES, generated_at=None, timeout=120):
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or _is_reparse(root):
        raise ValueError("project root must be a plain directory")
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    if python_info is None:
        python_path = Path(python_executable) if python_executable else root / PYTHON_REL
        python_path = python_path.resolve(strict=True)
        python_info = inspect_python(root, python_path, module_names=module_names, timeout=timeout)
    else:
        if not isinstance(python_info, dict) or not isinstance(python_info.get("packages"), list):
            raise ValueError("injected Python metadata has invalid shape")
        validate_packages(python_info["packages"])

    files_by_path = {}
    runtime_files = _collect_tree(root, Path("runtime"), "bundled_runtime", files_by_path)
    python_files = _collect_tree(root, RVC_REL / "runtime", "python_runtime", files_by_path)
    app_files = _collect_tree(root, Path("core"), "application_core", files_by_path)
    entry_path = Path("AI_Music_Processor.pyw")
    _collect_tree(root, entry_path, "application_entry", files_by_path)
    _collect_tree(root, Path("process_music.py"), "application_cli", files_by_path)
    vendor = _collect_vendor_records(root, files_by_path)
    models = _collect_model_records(root, files_by_path)
    from core.component_platform import load_catalog, catalog_path
    components = load_catalog(catalog_path(root))

    inventory_records = sorted(files_by_path.values(), key=lambda item: (item["path"].casefold(), item["path"]))
    for record in inventory_records:
        record["roles"].sort()
    inventory = {"schema_version": SCHEMA_VERSION, "files": inventory_records,
                 "cache_policy": {"excluded_directory": "__pycache__",
                                  "sourceless_bytecode": "included outside __pycache__"}}
    package_lock = {
        "schema_version": SCHEMA_VERSION,
        "runtime_python": {key: python_info[key] for key in (
            "implementation", "version", "version_full", "machine", "bits", "executable",
            "prefix", "base_prefix", "sys_path", "search_roots"
        )},
        "search_order": list(python_info["search_roots"]),
        "module_resolution": python_info["module_resolution"],
        "distributions": python_info["packages"],
        "rebuild_claim": "snapshot of the current installed bundle; no network pip rebuild was verified",
    }
    _check_locked_package_paths(root, package_lock)

    documents = {"inventory": inventory, "packages": package_lock, "vendor": vendor,
                 "models": models, "components": components}
    artifacts = {}
    for name, document in documents.items():
        artifacts[name] = {
            "path": "runtime-manifest/" + ("packages.lock.json" if name == "packages" else name + ".json"),
            "sha256": _digest_json(document),
        }
        if name == "inventory":
            artifacts[name]["file_count"] = len(inventory_records)
    generated = generated_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_kind": "current-installed-local-bundle",
        "generated_at_utc": generated,
        "distribution_status": "UNKNOWN; public distribution is not cleared by this snapshot",
        "source_status": "UNKNOWN",
        "license_status": "UNKNOWN",
        "python": {key: python_info[key] for key in (
            "implementation", "version", "version_full", "machine", "bits", "executable"
        )},
        "package_count": len(package_lock["distributions"]),
        "artifacts": artifacts,
    }
    summary["snapshot_sha256"] = _digest_json({key: value for key, value in summary.items()
                                                  if key != "generated_at_utc"})
    # A fresh freeze with identical contents is byte-for-byte reproducible.
    # Reuse the prior timestamp only when its content digest matches; changed
    # runtime contents receive the current timestamp as usual.
    if generated_at is None:
        candidate_dir = Path(output_dir)
        if not candidate_dir.is_absolute():
            candidate_dir = root / candidate_dir
        existing_summary_path = candidate_dir / "runtime.json"
        if not _is_reparse(existing_summary_path) and existing_summary_path.is_file():
            try:
                existing_summary = json.loads(existing_summary_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                existing_summary = None
            if (isinstance(existing_summary, dict)
                    and existing_summary.get("snapshot_sha256") == summary["snapshot_sha256"]
                    and isinstance(existing_summary.get("generated_at_utc"), str)):
                summary["generated_at_utc"] = existing_summary["generated_at_utc"]
    validate_snapshot(summary, inventory, package_lock, vendor, models, components)

    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    output_dir = output_dir.resolve()
    if not output_dir.is_relative_to(root):
        raise ValueError("manifest output directory must stay inside the project root")
    _atomic_json(output_dir / "inventory.json", inventory)
    _atomic_json(output_dir / "packages.lock.json", package_lock)
    _atomic_json(output_dir / "vendor.json", vendor)
    _atomic_json(output_dir / "models.json", models)
    _atomic_json(output_dir / "runtime.json", summary)
    return summary


def _check_locked_package_paths(root, package_lock):
    distributions = package_lock.get("distributions")
    validate_packages(distributions)
    seen_files = set()
    for package in distributions:
        for record in package["metadata_files"]:
            relative = record["path"]
            _check_safe_relative(relative)
            path = Path(root, relative)
            if not path.is_file() or _is_reparse(path):
                raise ValueError("package metadata file is missing or unsafe: " + relative)
            signature_before = _stat_signature(path)
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            if signature_before != _stat_signature(path):
                raise RuntimeError("package metadata changed during lock collection: " + relative)
            if signature_before[0] != record["size"] or hasher.hexdigest() != record["sha256"]:
                raise RuntimeError("package metadata differs from the runtime probe: " + relative)
            key = relative.casefold()
            if key in seen_files:
                continue
            seen_files.add(key)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="manifest directory (default: <root>/runtime-manifest)")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    output_dir = args.output_dir or root / "runtime-manifest"
    summary = build_snapshot(root, output_dir, timeout=args.timeout)
    print(json.dumps({
        "status": "FROZEN",
        "snapshot_sha256": summary["snapshot_sha256"],
        "files": summary["artifacts"]["inventory"]["file_count"],
        "packages": summary["package_count"],
        "distribution_status": summary["distribution_status"],
        "output_dir": str(output_dir),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
