"""Read-only component catalog, registry, and capability queries.

Presence means a declared probe exists. It does not mean the engine has run.
This module does not download files, write a component database, or import
the audio pipeline.
"""
import copy
import hashlib
import json
import os
import platform
import shutil
from pathlib import Path

from core.paths import is_release_mode

SCHEMA_VERSION = 1
CATALOG_RELATIVE = "runtime-manifest/components.json"
KINDS = ("application", "runtime", "tool", "engine", "model", "model_scan", "mode_pack")
OWNERSHIP = ("managed", "user", "external")
MODES = ("polish", "voice_conversion", "transcribe")
PROBE_TYPES = ("relative_file", "relative_dir", "tool", "user_rvc_models")
VERSION_PROBE_TYPES = ("json_keys", "text_file", "path_exists")
ID_PART = "abcdefghijklmnopqrstuvwxyz0123456789_"


class ComponentCatalogError(ValueError):
    """The declarative catalog contradicts its own schema."""


def catalog_path(root):
    return Path(root) / "runtime-manifest" / "components.json"


def load_catalog(path):
    with open(path, encoding="utf-8-sig") as handle:
        catalog = json.load(handle)
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog):
    if not isinstance(catalog, dict):
        raise ComponentCatalogError("catalog must be an object")
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise ComponentCatalogError("schema_version must be 1")
    if catalog.get("catalog_kind") != "installed-workspace-description":
        raise ComponentCatalogError("catalog_kind is not the workspace description catalog")
    if not isinstance(catalog.get("size_policy"), str) or not catalog["size_policy"].strip():
        raise ComponentCatalogError("size_policy is required")
    allowed = {"schema_version", "catalog_kind", "size_policy", "components"}
    extra = set(catalog) - allowed
    if extra:
        raise ComponentCatalogError("unknown catalog fields: " + ", ".join(sorted(extra)))
    components = catalog.get("components")
    if not isinstance(components, list) or not components:
        raise ComponentCatalogError("components must be a non-empty list")
    by_id = {}
    for component in components:
        _validate_component(component)
        if component["id"] in by_id:
            raise ComponentCatalogError("duplicate component id: " + component["id"])
        by_id[component["id"]] = component
    for component in components:
        for field in ("dependencies", "requires_any"):
            for dependency in component.get(field, []):
                if dependency not in by_id:
                    raise ComponentCatalogError(component["id"] + " references missing " + dependency)
                if dependency == component["id"]:
                    raise ComponentCatalogError(component["id"] + " depends on itself")
        _walk_dependencies(component["id"], by_id, [])
    return catalog


def _validate_component(component):
    if not isinstance(component, dict):
        raise ComponentCatalogError("component must be an object")
    required = {
        "id", "name", "kind", "ownership", "required", "implemented", "capabilities",
        "modes", "dependencies", "probes", "license_status", "distribution_status",
    }
    missing = required - set(component)
    if missing:
        raise ComponentCatalogError("component missing fields: " + ", ".join(sorted(missing)))
    allowed = required | {
        "version", "version_evidence", "version_probe", "requires_any", "details", "unavailable_reason",
    }
    extra = set(component) - allowed
    if extra:
        raise ComponentCatalogError("unknown component fields: " + ", ".join(sorted(extra)))
    if not _is_id(component["id"]):
        raise ComponentCatalogError("invalid component id: " + str(component["id"]))
    if not isinstance(component["name"], str) or not component["name"].strip():
        raise ComponentCatalogError(component["id"] + " needs a name")
    if component["kind"] not in KINDS:
        raise ComponentCatalogError(component["id"] + " has an unknown kind")
    if component["ownership"] not in OWNERSHIP:
        raise ComponentCatalogError(component["id"] + " has an unknown ownership")
    if not isinstance(component["required"], bool) or not isinstance(component["implemented"], bool):
        raise ComponentCatalogError(component["id"] + " required/implemented must be boolean")
    if component.get("license_status") != "UNKNOWN" or component.get("distribution_status") != "UNKNOWN":
        raise ComponentCatalogError(component["id"] + " must keep license and distribution status UNKNOWN")
    if "version" in component and component["version"] is not None and not isinstance(component["version"], str):
        raise ComponentCatalogError(component["id"] + " version must be a string or null")
    capabilities = component["capabilities"]
    if not isinstance(capabilities, list) or not capabilities or any(not _is_id(item) for item in capabilities):
        raise ComponentCatalogError(component["id"] + " needs capability ids")
    modes = component["modes"]
    if not isinstance(modes, list) or any(item not in MODES for item in modes):
        raise ComponentCatalogError(component["id"] + " has an unknown mode")
    for field in ("dependencies", "requires_any"):
        values = component.get(field, [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ComponentCatalogError(component["id"] + " " + field + " must be a list of ids")
    if component["implemented"] is False and not component.get("unavailable_reason"):
        raise ComponentCatalogError(component["id"] + " needs unavailable_reason")
    probes = component["probes"]
    if not isinstance(probes, list):
        raise ComponentCatalogError(component["id"] + " probes must be a list")
    if component["kind"] == "mode_pack" and probes:
        raise ComponentCatalogError(component["id"] + " mode pack cannot have file probes")
    if component["implemented"] and component["kind"] != "mode_pack" and not probes:
        raise ComponentCatalogError(component["id"] + " needs probes")
    if component["kind"] == "model_scan":
        if component["ownership"] != "user" or len(probes) != 1 or probes[0].get("type") != "user_rvc_models":
            raise ComponentCatalogError(component["id"] + " must be one user model scan")
    for probe in probes:
        _validate_probe(component["id"], probe)
    if "version_probe" in component:
        _validate_version_probe(component["id"], component["version_probe"])
    if "details" in component:
        _validate_details(component["id"], component["details"])


def _validate_details(component_id, details):
    if not isinstance(details, dict):
        raise ComponentCatalogError(component_id + " details must be an object")
    allowed = {"f0_methods", "f0_methods_not_implemented", "notes", "minimum_python_version"}
    extra = set(details) - allowed
    if extra:
        raise ComponentCatalogError(component_id + " has unknown details")
    for key in ("f0_methods", "f0_methods_not_implemented"):
        if key in details and (not isinstance(details[key], list) or any(not isinstance(item, str) for item in details[key])):
            raise ComponentCatalogError(component_id + " " + key + " must be strings")
    if "notes" in details and not isinstance(details["notes"], str):
        raise ComponentCatalogError(component_id + " notes must be a string")
    if "minimum_python_version" in details:
        version = details["minimum_python_version"]
        if (not isinstance(version, list) or len(version) != 2 or
                any(type(part) is not int or part < 0 for part in version)):
            raise ComponentCatalogError(component_id + " minimum_python_version must be [major, minor]")


def _validate_version_probe(component_id, probe):
    if not isinstance(probe, dict) or probe.get("type") not in VERSION_PROBE_TYPES:
        raise ComponentCatalogError(component_id + " has an invalid version probe")
    if set(probe) - {"type", "file", "keys", "path"}:
        raise ComponentCatalogError(component_id + " has unknown version probe fields")
    if probe["type"] in ("json_keys", "text_file"):
        _relative_path(probe.get("file"), component_id)
    if probe["type"] == "json_keys" and (not isinstance(probe.get("keys"), list) or not probe["keys"]):
        raise ComponentCatalogError(component_id + " json version probe needs keys")
    if probe["type"] == "path_exists":
        _relative_path(probe.get("path"), component_id)


def _validate_probe(component_id, probe):
    if not isinstance(probe, dict) or probe.get("type") not in PROBE_TYPES:
        raise ComponentCatalogError(component_id + " has an invalid probe")
    kind = probe["type"]
    allowed = {
        "relative_file": {"type", "path", "role"},
        "relative_dir": {"type", "path", "role"},
        "tool": {"type", "name", "release_path", "dev_paths", "allow_host_in_dev", "host_before_dev_path_index"},
        "user_rvc_models": {"type", "weights_dir", "logs_dir", "user_root", "managed_files"},
    }[kind]
    if set(probe) != allowed:
        raise ComponentCatalogError(component_id + " probe fields do not match " + kind)
    if kind in ("relative_file", "relative_dir"):
        _relative_path(probe.get("path"), component_id)
        if probe.get("role") not in ("required", "optional"):
            raise ComponentCatalogError(component_id + " probe role is invalid")
    elif kind == "tool":
        if not isinstance(probe.get("name"), str) or not probe["name"].strip():
            raise ComponentCatalogError(component_id + " tool probe needs a name")
        _relative_path(probe.get("release_path"), component_id)
        paths = probe.get("dev_paths")
        if not isinstance(paths, list) or not paths:
            raise ComponentCatalogError(component_id + " tool probe needs dev_paths")
        for item in paths:
            _relative_path(item, component_id)
        if not isinstance(probe.get("allow_host_in_dev"), bool):
            raise ComponentCatalogError(component_id + " allow_host_in_dev must be boolean")
        index = probe.get("host_before_dev_path_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ComponentCatalogError(component_id + " host_before_dev_path_index must be an integer")
    else:
        for field in ("weights_dir", "logs_dir", "user_root"):
            _relative_path(probe.get(field), component_id)
        files = probe.get("managed_files")
        if not isinstance(files, list) or not files or any(not isinstance(item, str) or "/" in item or "\\" in item for item in files):
            raise ComponentCatalogError(component_id + " managed_files must be file names")


def _relative_path(value, owner):
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")) or ":" in value or "\\" in value:
        raise ComponentCatalogError(owner + " path must be a relative forward-slash path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ComponentCatalogError(owner + " path escapes its root: " + value)
    return value


def _is_id(value):
    if not isinstance(value, str) or "." not in value:
        return False
    return all(
        part and part[0] in "abcdefghijklmnopqrstuvwxyz" and all(char in ID_PART for char in part)
        for part in value.split(".")
    )


def _walk_dependencies(component_id, by_id, stack):
    if component_id in stack:
        raise ComponentCatalogError("dependency cycle: " + " -> ".join(stack + [component_id]))
    stack.append(component_id)
    for dependency in by_id[component_id]["dependencies"]:
        _walk_dependencies(dependency, by_id, stack)
    stack.pop()


def dependency_closure(catalog, component_id):
    by_id = {item["id"]: item for item in catalog["components"]}
    if component_id not in by_id:
        raise ComponentCatalogError("unknown component: " + component_id)
    order = []

    def walk(current, stack):
        if current in stack:
            raise ComponentCatalogError("dependency cycle: " + current)
        if current in order:
            return
        stack.append(current)
        for dependency in by_id[current]["dependencies"]:
            walk(dependency, stack)
        stack.pop()
        order.append(current)

    walk(component_id, [])
    return [item for item in order if item != component_id]


def build_registry(root, catalog=None, release_mode=None, verify_hashes=False):
    """Recompute component status from the catalog and the current files."""
    root = Path(root).resolve()
    if catalog is None:
        loaded_from = catalog_path(root)
        catalog = load_catalog(loaded_from)
    else:
        validate_catalog(catalog)
        loaded_from = None
    if release_mode is None:
        release_mode = is_release_mode(root)
    models_index, models_status = _load_models_index(root)
    components = {}
    paths = {}
    warnings = []
    for component in catalog["components"]:
        record = _inspect_component(root, component, release_mode, models_index, verify_hashes, paths, warnings)
        components[component["id"]] = record
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "release_mode": bool(release_mode),
        "catalog_path": str(loaded_from) if loaded_from else None,
        "models_index_status": models_status,
        "components": components,
        "paths": paths,
        "warnings": warnings,
        "catalog": catalog,
        "component_order": [item["id"] for item in catalog["components"]],
        "verifier": runtime_verifier_binding(),
    }


def _inspect_component(root, component, release_mode, models_index, verify_hashes, paths, warnings):
    record = {
        "id": component["id"],
        "name": component["name"],
        "kind": component["kind"],
        "ownership": component["ownership"],
        "required": component["required"],
        "implemented": component["implemented"],
        "version": component.get("version"),
        "version_status": "NOT_APPLICABLE",
        "capabilities": list(component["capabilities"]),
        "status": "NOT_IMPLEMENTED" if not component["implemented"] else "UNRESOLVED",
        "probed_bytes": 0,
        "footprint": "PARTIAL_PROBES_ONLY",
        "files": [],
        "user_models": [],
        "missing_required": [],
        "missing_optional": [],
        "unavailable_reason": component.get("unavailable_reason"),
        "details": copy.deepcopy(component.get("details") or {}),
    }
    if not component["implemented"]:
        record["version_status"] = "NOT_APPLICABLE"
        return record
    if component["kind"] == "mode_pack":
        record["version_status"] = _version_status(root, component)
        return record
    if component['ownership']=='external' and component['id'] in (
            'tool.audacity','tool.musescore','tool.lmms','tool.neuralnote'):
        from core.component_location import resolve_editor_location
        from core.paths import application_data_paths
        try:
            chosen=resolve_editor_location(application_data_paths(root)['config'],
                                           component['id'].split('.',1)[1],None)
        except (OSError,ValueError) as error:
            record.update(status='UNAVAILABLE',unavailable_reason='自定义编辑器路径校验失败：'+str(error))
            warnings.append(record['unavailable_reason'])
            return record
        if chosen:
            found=_path_found(root,Path(chosen),False)
            _add_resolved_file(component,found,'required',models_index,verify_hashes,record,paths,warnings)
            record.update(status='PRESENT',version_status='UNVERIFIED')
            record['details'].update(location_source='user_selected_external_editor',import_status='NOT_ASSESSED')
            return record
    for probe in component["probes"]:
        if probe["type"] == "user_rvc_models":
            _inspect_user_models(root, component, probe, models_index, verify_hashes, record, paths, warnings)
            continue
        if probe["type"] == "tool":
            found = _resolve_tool(root, probe, release_mode)
            _add_resolved_file(component, found, "required", models_index, verify_hashes, record, paths, warnings)
            continue
        found = _resolve_relative(root, probe["path"], probe["type"] == "relative_dir")
        _add_resolved_file(component, found, probe["role"], models_index, verify_hashes, record, paths, warnings)
    minimum_python = (component.get("details") or {}).get("minimum_python_version")
    if minimum_python is not None:
        runtime_version = _runtime_python_version(root)
        required_text = ".".join(str(part) for part in minimum_python)
        if runtime_version is None:
            record["status"] = "UNKNOWN_RUNTIME"
            record["unavailable_reason"] = "无法从 runtime-manifest/runtime.json 解析 bundled Python；要求 Python >= " + required_text
        elif runtime_version < tuple(minimum_python):
            actual_text = ".".join(str(part) for part in runtime_version)
            record["status"] = "INCOMPATIBLE_RUNTIME"
            record["unavailable_reason"] = "bundled Python " + actual_text + " 低于要求 >= " + required_text
        elif record["missing_required"]:
            record["status"] = "OPTIONAL_ABSENT" if component["ownership"] == "external" else "MISSING"
        elif component["kind"] == "model_scan" and not record["user_models"]:
            record["status"] = "ABSENT"
        else:
            record["status"] = "PRESENT"
    elif record["missing_required"]:
        record["status"] = "OPTIONAL_ABSENT" if component["ownership"] == "external" else "MISSING"
    elif component["kind"] == "model_scan" and not record["user_models"]:
        record["status"] = "ABSENT"
    else:
        record["status"] = "PRESENT"
    record["version_status"] = _version_status(root, component)
    return record


def _runtime_python_version(root):
    """Read the bundled interpreter version from the frozen runtime summary."""
    path = Path(root) / "runtime-manifest" / "runtime.json"
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            summary = json.load(handle)
        raw = summary.get("python", {}).get("version")
        if not isinstance(raw, str):
            return None
        parts = raw.split(".")
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        return int(parts[0]), int(parts[1])
    except (OSError, UnicodeError, ValueError, AttributeError, TypeError):
        return None


def _add_resolved_file(component, found, role, models_index, verify_hashes, record, paths, warnings):
    file_record = _file_record(found, models_index, verify_hashes)
    record["files"].append(file_record)
    if file_record["exists"] and file_record["size"] is not None:
        record["probed_bytes"] += file_record["size"]
    if file_record["exists"] and file_record["relative"] and file_record["inside_root"]:
        owners = paths.setdefault(file_record["relative"], [])
        if component["id"] not in owners:
            owners.append(component["id"])
        if len(owners) > 1:
            message = file_record["relative"] + " is claimed by " + ", ".join(owners)
            if message not in warnings:
                warnings.append(message)
    elif not file_record["exists"]:
        key = "missing_required" if role == "required" else "missing_optional"
        record[key].append(file_record["relative"] or file_record["path"])


def _resolve_relative(root, relative, directory):
    path = root.joinpath(*relative.split("/"))
    inside = _is_inside(root, path)
    exists = path.is_dir() if directory else path.is_file()
    if exists and not inside:
        exists = False
    size = None
    if exists and not directory:
        size = path.stat().st_size
    return {"path": str(path), "relative": relative, "inside_root": inside, "exists": exists, "size": size, "directory": directory}


def _resolve_tool(root, probe, release_mode):
    if release_mode:
        return _resolve_relative(root, probe["release_path"], False)
    sequence = []
    host_index = probe["host_before_dev_path_index"]
    for index, relative in enumerate(probe["dev_paths"]):
        if probe["allow_host_in_dev"] and index == host_index:
            sequence.append(("host", None))
        sequence.append(("relative", relative))
    if probe["allow_host_in_dev"] and host_index >= len(probe["dev_paths"]):
        sequence.append(("host", None))
    for kind, relative in sequence:
        if kind == "host":
            host = _host_tool(probe["name"])
            if host:
                size = os.path.getsize(host) if os.path.isfile(host) else None
                return {"path": host, "relative": None, "inside_root": False, "exists": True, "size": size, "directory": False}
            continue
        found = _resolve_relative(root, relative, False)
        if found["exists"]:
            return found
    return _resolve_relative(root, probe["dev_paths"][0], False)


def _host_tool(name):
    """Follow the development candidates used by core.config.resolve_tool_path."""
    found = shutil.which(name)
    if found and os.path.isfile(found):
        return found
    windows = os.path.join("C:\\ffmpeg\\bin", name + ".exe")
    if os.path.isfile(windows):
        return windows
    return None


def _inspect_user_models(root, component, probe, models_index, verify_hashes, record, paths, warnings):
    managed = set(probe["managed_files"])
    user_root = root.joinpath(*probe["user_root"].split("/"))
    if user_root.is_dir() and _is_inside(root, user_root):
        for child in sorted(path for path in user_root.iterdir() if path.is_dir()):
            if not _is_inside(root, child):
                continue
            weights = sorted(path for path in child.iterdir() if path.is_file() and path.suffix.lower() == ".pth")
            indices = sorted(
                path for path in child.iterdir()
                if path.is_file() and path.suffix.lower() == ".index" and not path.name.startswith("trained_")
            )
            if weights:
                _append_user_model(root, component, child.name, weights[0], indices[0] if indices else None,
                                   models_index, verify_hashes, record, paths, warnings)
    weights_dir = root.joinpath(*probe["weights_dir"].split("/"))
    logs_dir = root.joinpath(*probe["logs_dir"].split("/"))
    if weights_dir.is_dir() and _is_inside(root, weights_dir):
        for path in sorted(path for path in weights_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pth"):
            if path.name in managed or not _is_inside(root, path):
                continue
            index_path = _first_log_index(logs_dir / path.stem)
            _append_user_model(root, component, path.stem, path, index_path, models_index, verify_hashes, record, paths, warnings)


def _first_log_index(directory):
    """Match get_available_rvc_models: the first os.listdir name that ends in .index."""
    if not directory.is_dir():
        return None
    for name in os.listdir(directory):
        if name.endswith(".index"):
            return directory / name
    return None


def _append_user_model(root, component, key, weight, index_path, models_index, verify_hashes, record, paths, warnings):
    weight_record = _file_record(_path_found(root, weight, False), models_index, verify_hashes)
    index_record = None
    if index_path is not None:
        index_record = _file_record(_path_found(root, index_path, False), models_index, verify_hashes)
    record["files"].append(weight_record)
    if index_record:
        record["files"].append(index_record)
    for file_record in (weight_record, index_record):
        if not file_record or not file_record["exists"]:
            continue
        if file_record["size"] is not None:
            record["probed_bytes"] += file_record["size"]
        if file_record["relative"] and file_record["inside_root"]:
            owners = paths.setdefault(file_record["relative"], [])
            if component["id"] not in owners:
                owners.append(component["id"])
    record["user_models"].append({
        "key": key,
        "ownership": "user",
        "pth": weight_record["relative"],
        "index": index_record["relative"] if index_record and index_record["exists"] else None,
        "bytes": (weight_record["size"] or 0) + ((index_record or {}).get("size") or 0),
    })


def _path_found(root, path, directory):
    inside = _is_inside(root, path)
    exists = path.is_dir() if directory else path.is_file()
    if exists and not inside:
        exists = False
    relative = None
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        relative = None
    size = path.stat().st_size if exists and not directory else None
    return {"path": str(path), "relative": relative, "inside_root": inside, "exists": exists, "size": size, "directory": directory}


def _file_record(found, models_index, verify_hashes):
    recorded = models_index.get(found["relative"]) if found["relative"] else None
    record = {
        "path": found["path"],
        "relative": found["relative"],
        "inside_root": found["inside_root"],
        "exists": found["exists"],
        "directory": found["directory"],
        "size": found["size"],
        "recorded_sha256": recorded.get("sha256") if recorded else None,
        "recorded_size": recorded.get("size") if recorded else None,
        "sha256": None,
        "hash_status": "NOT_CHECKED",
    }
    if recorded and found["exists"] and not found["directory"] and isinstance(recorded.get("size"), int):
        record["hash_status"] = "RECORDED_SIZE_MATCH" if found["size"] == recorded["size"] else "STALE_RECORD"
    elif recorded and not found["exists"]:
        record["hash_status"] = "MISSING_FILE"
    if verify_hashes and found["exists"] and not found["directory"] and found["inside_root"]:
        digest = _sha256(found["path"])
        record["sha256"] = digest
        if record["recorded_sha256"]:
            record["hash_status"] = "VERIFIED" if digest == record["recorded_sha256"] else "HASH_MISMATCH"
        else:
            record["hash_status"] = "HASHED_NO_RECORD"
    return record


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_status(root, component):
    probe = component.get("version_probe")
    expected = component.get("version")
    if not probe:
        return "NOT_APPLICABLE" if not expected else "UNVERIFIED"
    if probe["type"] == "text_file":
        path = root.joinpath(*probe["file"].split("/"))
        if not path.is_file() or not _is_inside(root, path):
            return "UNVERIFIED"
        actual = path.read_text(encoding="utf-8-sig").strip()
        return "MATCH" if actual == expected else "MISMATCH"
    if probe["type"] == "json_keys":
        path = root.joinpath(*probe["file"].split("/"))
        if not path.is_file() or not _is_inside(root, path):
            return "UNVERIFIED"
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            for key in probe["keys"]:
                value = value[key]
        except (OSError, ValueError, KeyError, TypeError):
            return "UNVERIFIED"
        return "MATCH" if value == expected else "MISMATCH"
    found = _resolve_relative(root, probe["path"], True)
    if not found["exists"]:
        found = _resolve_relative(root, probe["path"], False)
    return "MATCH" if found["exists"] else "UNVERIFIED"


def _load_models_index(root):
    path = root / "runtime-manifest" / "models.json"
    if not path.is_file():
        return {}, "ABSENT"
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}, "UNREADABLE"
    index = {}
    for asset in document.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        for item in asset.get("files") or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            index[item["path"]] = {"sha256": item.get("sha256"), "size": item.get("size")}
    return index, "LOADED"


def _is_inside(root, path):
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def capability_report(registry, runtime_verification=None):
    """Resolve mode packs from registry status. This function changes nothing."""
    catalog = registry["catalog"]
    components = registry["components"]
    providers = {}
    for component in components.values():
        for capability in component["capabilities"]:
            providers.setdefault(capability, []).append({
                "id": component["id"],
                "status": component["status"],
                "implemented": component["implemented"],
            })
    capabilities = {}
    for name, items in sorted(providers.items()):
        capabilities[name] = {
            "available": any(item["implemented"] and item["status"] == "PRESENT" for item in items),
            "providers": items,
        }
    modes = {}
    allowed_by_gate = None
    if isinstance(runtime_verification, dict):
        if runtime_verification.get("status") in ("PASS", "PASS_WITH_WARNINGS"):
            allowed_by_gate = set(runtime_verification.get("allowed_modes") or MODES)
        elif runtime_verification.get("status") == "FAIL":
            allowed_by_gate = set()

    for component in catalog["components"]:
        if component["kind"] != "mode_pack":
            continue
        closure = dependency_closure(catalog, component["id"])
        missing = [item for item in closure if components[item]["status"] != "PRESENT"]
        group = component.get("requires_any") or []
        group_ready = [item for item in group if components[item]["status"] == "PRESENT"]
        blocked = bool(missing) or (bool(group) and not group_ready) or not component["implemented"]
        mode_name = component["modes"][0]

        missing_reasons = []
        user_import_required = False
        missing_components = False

        for item in missing:
            comp_spec = components.get(item, {})
            kind = comp_spec.get("kind", "")
            name = comp_spec.get("name", item)
            ownership = comp_spec.get("ownership", "")
            if kind == "model_scan" or ownership == "user" or "model" in item:
                user_import_required = True
                missing_reasons.append(f"需要导入模型权重: {name}")
            elif kind == "runtime":
                missing_components = True
                missing_reasons.append(f"缺少运行时环境: {name}")
            else:
                missing_components = True
                missing_reasons.append(f"缺少组件: {name}")

        if not group_ready and group:
            missing_reasons.append(f"缺少必要组件依赖组 (任选其一): {', '.join(group)}")
            missing_components = True

        gate_disallowed = (allowed_by_gate is not None and mode_name not in allowed_by_gate)

        if allowed_by_gate is not None and len(allowed_by_gate) == 0:
            display_status = "FAILED"
            blocked = True
        elif gate_disallowed:
            blocked = True
            if user_import_required and not missing_components:
                display_status = "USER_IMPORT_REQUIRED"
            else:
                display_status = "MISSING_COMPONENTS"
            if not missing_reasons:
                missing_reasons.append(f"当前运行环境未授权模式 '{mode_name}'，需完整组件或 donor 离线安装")
        elif blocked:
            if user_import_required and not missing_components:
                display_status = "USER_IMPORT_REQUIRED"
            else:
                display_status = "MISSING_COMPONENTS"
        else:
            display_status = "READY"

        modes[mode_name] = {
            "component_id": component["id"],
            "status": "BLOCKED" if blocked else "AVAILABLE",
            "display_status": display_status,
            "missing": missing,
            "missing_reasons": missing_reasons,
            "missing_any_group": [] if group_ready or not group else list(group),
            "dependencies": closure,
            "allowed_by_gate": not gate_disallowed,
        }
    shared = {}
    for component_id in registry["component_order"]:
        users = [info["component_id"] for info in modes.values() if component_id in info["dependencies"]]
        if len(users) > 1:
            shared[component_id] = users
    return {"capabilities": capabilities, "modes": modes, "shared_dependencies": shared}


def has_capability(report, name):
    item = report["capabilities"].get(name)
    return bool(item and item["available"])


def dependents(report, component_id):
    """Return mode packs that need this component. This does not delete anything."""
    return [info["component_id"] for info in report["modes"].values() if component_id in info["dependencies"]]


def classify_voice_model(relative_pth, managed_files, weights_dir, user_root):
    """Classify one weight path without moving it.

    models/rvc belongs to the user. Extra .pth files dropped into the managed
    weights directory also stay user-owned. The three known filenames at that
    weights directory are managed.
    """
    relative = relative_pth.replace("\\", "/")
    name = relative.rsplit("/", 1)[-1]
    user_prefix = user_root.strip("/") + "/"
    weights_prefix = weights_dir.strip("/") + "/"
    if relative.startswith(user_prefix):
        return "user"
    if relative.startswith(weights_prefix) and name in set(managed_files):
        return "managed"
    return "user"


def model_profile_contract():
    """Expose the existing inference profile. Do not create a second store."""
    from core.config import MODEL_PROFILE_DEFAULTS, _PROFILE_RANGES, _validate_model_profile
    accepted = []
    for name in ("harvest", "pm", "crepe", "rmvpe"):
        try:
            _validate_model_profile({"f0_method": name})
        except ValueError:
            continue
        accepted.append(name)
    return {
        "source": "core.config.MODEL_PROFILE_DEFAULTS",
        "second_profile_system": False,
        "job_spec_schema_version": 1,
        "defaults": copy.deepcopy(MODEL_PROFILE_DEFAULTS),
        "ranges": {key: [low, high] for key, (low, high) in _PROFILE_RANGES.items()},
        "f0_methods_accepted_by_config": accepted,
        "f0_methods_not_implemented": [],
    }


def scan_hardware(path=None):
    """Read OS, CPU, memory, and free disk. Do not import Torch."""
    probe = Path(path or os.getcwd())
    usage = shutil.disk_usage(probe if probe.exists() else Path.cwd())
    total, available = _physical_memory()
    return {
        "os": os.name,
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "ram_total_bytes": total,
        "ram_available_bytes": available,
        "disk_free_bytes": usage.free,
        "disk_total_bytes": usage.total,
        "disk_probe_path": str(probe),
        "gpu": {
            "status": "NOT_PROBED",
            "reason": "CUDA readiness stays in diagnostics() and verify_runtime(). This scan does not import torch.",
        },
    }


def _physical_memory():
    if os.name != "nt":
        return None, None
    import ctypes
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def runtime_verifier_binding():
    return {
        "module": "core.runtime_verify",
        "function": "verify_runtime",
        "invoked_by_registry": False,
        "reason": "Full inventory hashing remains the portable release gate. The component registry only recomputes declared probes.",
    }
