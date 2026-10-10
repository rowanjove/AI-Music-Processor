"""Authenticated local component packages (``.ampkg``).

The portable release format is intentionally separate from this format.  An
AMPKG is a small, hash-pinned ZIP containing files selected from the current
managed component catalog and inventory.  It is suitable for a local repair
or component transfer; it never writes the live application tree.  Callers
must pass the extracted directory to :func:`core.install_platform.install_offline_package`
after any application specific transaction/maintenance checks.

Unsigned packages are *local inventory pins*, not vendor signatures.  RSA
signing is optional and verification only trusts public keys supplied by the
caller.  A public key embedded in a package is never trusted automatically.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from core.component_platform import dependency_closure, load_catalog


SCHEMA_VERSION = 1
PACKAGE_KIND = "ampkg"
MANIFEST_NAME = "manifest.json"
PAYLOAD_PREFIX = "payload/"
RECEIPT_NAME = "ampkg-receipt.json"
UNSIGNED_STATUS = "LOCAL_PINNED_HASHES_NOT_SIGNATURE"
SIGNED_STATUS = "SIGNED_RSA_SHA256"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 250_000
MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ComponentPackageError(ValueError):
    """The package is malformed, unauthorised, or cannot be verified."""


class ComponentPackageCancelled(ComponentPackageError):
    """The caller cancelled a package operation before publication."""


def _cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    return bool(getattr(cancel, "is_set", lambda: False)())


def _checkpoint(cancel) -> None:
    if _cancelled(cancel):
        raise ComponentPackageCancelled("AMPKG 操作已取消")


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_path(path: Path, *, allow_missing=True) -> Path:
    """Reject symlink/reparse points in an existing path and its ancestors."""
    path = Path(os.path.abspath(os.fspath(path)))
    current = path if (path.exists() or not allow_missing) else path.parent
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if current.is_symlink() or bool(getattr(info, "st_file_attributes", 0)
                                             & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise ComponentPackageError("AMPKG 路径不能经过符号链接或 junction：" + str(current))
        parent = current.parent
        if parent == current:
            break
        current = parent
    return path


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ComponentPackageError("AMPKG 清单包含非法相对路径")
    parts = value.split("/")
    parsed = PurePosixPath(value)
    if (parsed.is_absolute() or re.match(r"^[A-Za-z]:", value)
            or any(part in ("", ".", "..") for part in parts)):
        raise ComponentPackageError("AMPKG 路径越界：" + value)
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if (stem in _WINDOWS_DEVICES or part.endswith((".", " "))
                or any(ord(char) < 32 or char in '<>:"|?*' for char in part)):
            raise ComponentPackageError("AMPKG 路径含 Windows 非法名称：" + value)
    return value


def _key(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _version(root: Path) -> str:
    path = root / "VERSION"
    if not path.is_file():
        raise ComponentPackageError("当前根目录缺少 VERSION")
    value = path.read_text(encoding="utf-8-sig").strip()
    if not value or len(value) > 256:
        raise ComponentPackageError("VERSION 无效")
    return value


def _read_inventory(root: Path):
    path = root / "runtime-manifest" / "inventory.json"
    if not path.is_file():
        raise ComponentPackageError("缺少当前可信 inventory.json，拒绝生成 AMPKG")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComponentPackageError("当前 inventory.json 无法读取") from exc
    records = document.get("files") if isinstance(document, dict) else None
    if not isinstance(records, list) or not records or len(records) > MAX_FILES:
        raise ComponentPackageError("当前 inventory 文件清单无效")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ComponentPackageError("当前 inventory 记录无效")
        relative = _safe_relative(record.get("path"))
        digest = record.get("sha256")
        size = record.get("size")
        if (_key(relative) in result or not SHA256_RE.fullmatch(digest or "")
                or type(size) is not int or size < 0 or size > MAX_FILE_BYTES):
            raise ComponentPackageError("当前 inventory 路径、大小或 SHA 无效：" + relative)
        result[_key(relative)] = {"path": relative, "size": size, "sha256": digest}
    return result


def _component_selection(root: Path, requested):
    if isinstance(requested, str):
        requested = [item.strip() for item in requested.split(",") if item.strip()]
    if not isinstance(requested, (list, tuple)) or not requested:
        raise ComponentPackageError("至少选择一个组件")
    catalog_path = root / "runtime-manifest" / "components.json"
    try:
        catalog = load_catalog(catalog_path)
    except Exception as exc:
        raise ComponentPackageError("当前组件目录无效") from exc
    by_id = {item["id"]: item for item in catalog["components"]}
    selected = []
    for component_id in requested:
        if not isinstance(component_id, str) or component_id not in by_id:
            raise ComponentPackageError("未知组件：" + str(component_id))
        if component_id not in selected:
            selected.append(component_id)
    resolved = []
    for component_id in selected:
        item = by_id[component_id]
        if item["ownership"] != "managed" or not item["implemented"]:
            raise ComponentPackageError("AMPKG 只能包含当前已实现 managed 组件：" + component_id)
        for dependency in dependency_closure(catalog, component_id):
            dependency_item = by_id[dependency]
            if dependency_item["ownership"] != "managed" or not dependency_item["implemented"]:
                raise ComponentPackageError("组件依赖不可打包：" + dependency)
            if dependency not in resolved and dependency not in selected:
                resolved.append(dependency)
    return catalog, selected, resolved, by_id


def _probe_paths(root: Path, component, inventory):
    paths = []
    for probe in component.get("probes", []):
        kind = probe.get("type")
        if kind in ("relative_file", "tool"):
            relative = probe.get("path") if kind == "relative_file" else probe.get("release_path")
            if relative:
                paths.append(relative)
        elif kind == "relative_dir":
            prefix = _key(probe["path"].rstrip("/")) + "/"
            paths.extend(record["path"] for key, record in inventory.items() if key.startswith(prefix))
        elif kind == "user_rvc_models":
            raise ComponentPackageError("用户 RVC 模型不能进入 managed AMPKG")
    return paths


def _authorized_component_records(catalog, component_ids, by_id, inventory):
    """Return inventory records actually owned by the selected probe closure."""
    all_ids = list(dict.fromkeys(component_ids))
    # ``engine.rvc`` has a broad directory probe for its code/runtime.  That
    # directory also contains user-imported voice weights and indexes.  A
    # user model is allowed only when it is an explicit managed catalog
    # probe (for example a built-in model component selected by the caller),
    # never merely because a parent directory probe happened to include it.
    user_roots = []
    explicit = set()
    for component_id in all_ids:
        for probe in by_id[component_id].get("probes", []):
            kind = probe.get("type")
            if kind in ("relative_file", "tool"):
                relative = probe.get("path") if kind == "relative_file" else probe.get("release_path")
                if relative:
                    explicit.add(_key(relative))
    for component in catalog["components"]:
        for probe in component.get("probes", []):
            if probe.get("type") == "user_rvc_models":
                user_roots.extend((_key(probe["weights_dir"].rstrip("/")) + "/",
                                   _key(probe["logs_dir"].rstrip("/")) + "/"))
    records = {}
    for component_id in all_ids:
        component = by_id[component_id]
        for probe in component.get("probes", []):
            if probe.get("type") == "relative_dir":
                prefix = _key(probe["path"].rstrip("/")) + "/"
                candidates = (record["path"] for key, record in inventory.items()
                              if key.startswith(prefix))
            else:
                candidates = _probe_paths(None, {"probes": [probe]}, inventory)
            for relative in candidates:
                if (probe.get("type") == "relative_dir"
                        and any(_key(relative).startswith(user_root) for user_root in user_roots)
                        and _key(relative) not in explicit):
                    continue
                relative = _safe_relative(relative)
                record = inventory.get(_key(relative))
                if record is None:
                    raise ComponentPackageError("组件文件不在当前可信 inventory：" + relative)
                records[_key(relative)] = dict(record)
    if not records:
        raise ComponentPackageError("所选组件没有可打包的 managed 文件")
    result = list(records.values())
    result.sort(key=lambda item: (_key(item["path"]), item["path"]))
    if len(result) > MAX_FILES or sum(item["size"] for item in result) > MAX_TOTAL_BYTES:
        raise ComponentPackageError("AMPKG 文件数量或总大小超过上限")
    return result


def _file_records(root: Path, catalog, component_ids, by_id, inventory, cancel=None):
    records = _authorized_component_records(catalog, component_ids, by_id, inventory)
    for record in records:
        _checkpoint(cancel)
        relative = record["path"]
        source = root / Path(*relative.split("/"))
        _plain_path(source, allow_missing=False)
        if not source.is_file():
            raise ComponentPackageError("组件文件缺失：" + relative)
        stat_result = source.stat()
        if stat_result.st_size != record["size"] or _sha256_file(source) != record["sha256"]:
            raise ComponentPackageError("组件文件与当前 inventory 不一致：" + relative)
    return records


def _signature_material(manifest):
    unsigned = dict(manifest)
    signature = dict(unsigned.get("signature") or {})
    signature["value"] = ""
    unsigned["signature"] = signature
    return _json_bytes(unsigned)


def _load_rsa(value, *, private=False):
    try:
        from Crypto.PublicKey import RSA
    except ImportError as exc:
        raise ComponentPackageError("RSA 签名需要当前 runtime 的 PyCryptodome") from exc
    if hasattr(value, "export_key"):
        key = value
    else:
        raw = Path(value).read_bytes() if isinstance(value, (str, os.PathLike)) else bytes(value)
        key = RSA.import_key(raw)
    if private and not key.has_private():
        raise ComponentPackageError("signing key 必须是 RSA 私钥")
    if not private and key.has_private():
        key = key.publickey()
    if key.size_in_bits() < 2048:
        raise ComponentPackageError("RSA key 小于 2048 bit")
    return key


def _key_id(key) -> str:
    return _sha256_bytes(key.publickey().export_key(format="DER"))


def _sign_manifest(manifest, signing_key):
    key = _load_rsa(signing_key, private=True)
    from Crypto.Hash import SHA256
    from Crypto.Signature import pkcs1_15
    manifest["signature"] = {
        "status": SIGNED_STATUS,
        "algorithm": "RSA-SHA256",
        "key_id": _key_id(key),
        "value": "",
    }
    digest = SHA256.new(_signature_material(manifest))
    manifest["signature"]["value"] = base64.b64encode(pkcs1_15.new(key).sign(digest)).decode("ascii")
    return manifest


def _trusted_keys(trusted_public_keys):
    if not trusted_public_keys:
        raise ComponentPackageError("已签名 AMPKG 必须显式提供 trusted_public_keys")
    if isinstance(trusted_public_keys, dict):
        values = list(trusted_public_keys.items())
    else:
        values = [(None, value) for value in trusted_public_keys]
    result = {}
    for key_id, value in values:
        key = _load_rsa(value, private=False)
        actual = _key_id(key)
        if key_id is not None and key_id != actual:
            raise ComponentPackageError("trusted public key id 不匹配")
        result[actual] = key
    return result


def _verify_signature(manifest, trusted_public_keys=None):
    signature = manifest.get("signature")
    if not isinstance(signature, dict):
        raise ComponentPackageError("AMPKG 缺少 signature 状态")
    status = signature.get("status")
    if status == UNSIGNED_STATUS:
        if set(signature) != {"status"}:
            raise ComponentPackageError("无签名状态包含未知字段")
        return status
    if status != SIGNED_STATUS or set(signature) != {"status", "algorithm", "key_id", "value"}:
        raise ComponentPackageError("AMPKG 签名状态无效")
    if signature.get("algorithm") != "RSA-SHA256" or not isinstance(signature.get("key_id"), str):
        raise ComponentPackageError("AMPKG RSA 签名元数据无效")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
    except Exception as exc:
        raise ComponentPackageError("AMPKG RSA 签名编码无效") from exc
    keys = _trusted_keys(trusted_public_keys)
    key = keys.get(signature["key_id"])
    if key is None:
        raise ComponentPackageError("AMPKG 签名 key_id 不在 trusted_public_keys")
    try:
        from Crypto.Hash import SHA256
        from Crypto.Signature import pkcs1_15
        pkcs1_15.new(key).verify(SHA256.new(_signature_material(manifest)), signature_bytes)
    except Exception as exc:
        raise ComponentPackageError("AMPKG RSA 签名校验失败") from exc
    return status


def _validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ComponentPackageError("AMPKG manifest schema 无效")
    allowed = {
        "schema_version", "package_kind", "package_id", "version",
        "component_ids", "dependencies", "app_compatibility",
        "license_status", "distribution_status", "signature", "files",
    }
    if set(manifest) != allowed:
        raise ComponentPackageError("AMPKG manifest 字段集合无效")
    if manifest.get("package_kind") != PACKAGE_KIND:
        raise ComponentPackageError("AMPKG package_kind 无效")
    package_id = manifest.get("package_id")
    if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
        raise ComponentPackageError("AMPKG package_id 无效")
    version = manifest.get("version")
    if not isinstance(version, str) or not version or len(version) > 256:
        raise ComponentPackageError("AMPKG version 无效")
    selected = manifest.get("component_ids")
    dependencies = manifest.get("dependencies")
    component_id_re = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
    if (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
            or any(not isinstance(item, str) or not component_id_re.fullmatch(item) for item in selected)
            or not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies)
            or any(not isinstance(item, str) or not component_id_re.fullmatch(item) for item in dependencies)
            or set(selected) & set(dependencies)):
        raise ComponentPackageError("AMPKG component_ids/dependencies 无效")
    compatibility = manifest.get("app_compatibility")
    if (not isinstance(compatibility, dict)
            or set(compatibility) - {"app_version", "catalog_schema_version", "min_app_version", "max_app_version"}
            or compatibility.get("catalog_schema_version") != 1):
        raise ComponentPackageError("AMPKG app_compatibility 无效")
    if manifest.get("license_status") != "UNKNOWN" or manifest.get("distribution_status") != "UNKNOWN":
        raise ComponentPackageError("当前本地组件包不能宣称已知可再分发许可")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ComponentPackageError("AMPKG files 清单无效")
    seen = set()
    total = 0
    normalized = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ComponentPackageError("AMPKG 文件记录不完整")
        relative = _safe_relative(item["path"])
        key = _key(relative)
        size, digest = item["size"], item["sha256"]
        if (key in seen or type(size) is not int or size < 0 or size > MAX_FILE_BYTES
                or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
            raise ComponentPackageError("AMPKG 文件路径、大小或 SHA 无效：" + relative)
        seen.add(key)
        total += size
        normalized.append({"path": relative, "size": size, "sha256": digest})
    if total > MAX_TOTAL_BYTES:
        raise ComponentPackageError("AMPKG 文件总大小超过上限")
    if normalized != sorted(normalized, key=lambda item: (_key(item["path"]), item["path"])):
        raise ComponentPackageError("AMPKG 文件清单必须按路径稳定排序")
    return normalized


def _catalog_check(root: Path, manifest, *, current_version=True):
    catalog, selected, dependencies, by_id = _component_selection(root, manifest["component_ids"])
    if selected != manifest["component_ids"]:
        raise ComponentPackageError("AMPKG component_ids 顺序或重复不符合当前目录")
    if dependencies != manifest["dependencies"]:
        raise ComponentPackageError("AMPKG dependencies 不符合当前组件依赖闭包")
    if current_version and manifest["version"] != _version(root):
        raise ComponentPackageError("AMPKG 版本与当前 inventory 不同，不能覆盖当前版本")
    return catalog, by_id


def _inventory_check(root: Path, records, *, authorized_paths=None):
    inventory = _read_inventory(root)
    for record in records:
        current = inventory.get(_key(record["path"]))
        if current is None or current["size"] != record["size"] or current["sha256"] != record["sha256"]:
            raise ComponentPackageError("AMPKG 文件不匹配当前可信 inventory：" + record["path"])
        if authorized_paths is not None and _key(record["path"]) not in authorized_paths:
            raise ComponentPackageError("AMPKG payload 不属于所选组件 probe：" + record["path"])
    return inventory


def _zip_member_name(relative):
    return PAYLOAD_PREFIX + relative


def _zip_info_is_regular(info: zipfile.ZipInfo):
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        return False
    # Many ordinary ZIP writers encode only Unix permission bits (for
    # example 0600) and omit the file type bits.  Treat that as regular; a
    # declared directory is still rejected by ``is_dir`` above.
    file_type = stat.S_IFMT(mode)
    if mode and file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        return False
    return not info.is_dir()


def _read_zip_manifest(package_path: Path):
    try:
        archive = zipfile.ZipFile(package_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ComponentPackageError("AMPKG ZIP 无法打开") from exc
    try:
        infos = archive.infolist()
        if len(infos) > MAX_FILES + 1:
            raise ComponentPackageError("AMPKG ZIP 文件数量超过上限")
        names = {}
        for info in infos:
            if "\\" in info.filename or "\x00" in info.filename:
                raise ComponentPackageError("AMPKG ZIP 含非法成员路径")
            key = info.filename.casefold()
            if key in names:
                raise ComponentPackageError("AMPKG ZIP 含重复或大小写别名成员")
            names[key] = info
            if info.filename == MANIFEST_NAME:
                if info.file_size > MAX_MANIFEST_BYTES:
                    raise ComponentPackageError("AMPKG manifest 过大")
            elif not info.filename.startswith(PAYLOAD_PREFIX) or not _zip_info_is_regular(info):
                raise ComponentPackageError("AMPKG ZIP 含未知或特殊成员：" + info.filename)
            else:
                _safe_relative(info.filename[len(PAYLOAD_PREFIX):])
        manifest_info = names.get(MANIFEST_NAME.casefold())
        if manifest_info is None or manifest_info.filename != MANIFEST_NAME:
            raise ComponentPackageError("AMPKG 缺少唯一 manifest.json")
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ComponentPackageError("AMPKG manifest 无法读取") from exc
        records = _validate_manifest(manifest)
        expected = {(_zip_member_name(record["path"])).casefold(): record for record in records}
        actual = {key: info for key, info in names.items() if key != MANIFEST_NAME.casefold()}
        if set(expected) != set(actual):
            raise ComponentPackageError("AMPKG payload 成员与 manifest.files 不一致")
        for record in records:
            expected_name = _zip_member_name(record["path"])
            info = actual[expected_name.casefold()]
            if info.filename != expected_name:
                raise ComponentPackageError("AMPKG payload 路径大小写与 manifest 不一致：" + record["path"])
            if info.file_size != record["size"]:
                raise ComponentPackageError("AMPKG payload 大小不匹配：" + record["path"])
        return archive, manifest, records
    except Exception:
        archive.close()
        raise


def _hash_zip_payload(archive, records, cancel=None):
    """Hash payload only after manifest/signature/authority checks completed."""
    total = 0
    for record in records:
        _checkpoint(cancel)
        info = archive.getinfo(_zip_member_name(record["path"]))
        digest = hashlib.sha256()
        size = 0
        with archive.open(info, "r") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                _checkpoint(cancel)
                digest.update(chunk)
                size += len(chunk)
                total += len(chunk)
                if total > MAX_TOTAL_BYTES:
                    raise ComponentPackageError("AMPKG 实际 payload 总大小超过上限")
        if size != record["size"] or digest.hexdigest() != record["sha256"]:
            raise ComponentPackageError("AMPKG payload SHA-256 校验失败：" + record["path"])
    if total != sum(item["size"] for item in records):
        raise ComponentPackageError("AMPKG payload 总大小与 manifest 不一致")
    return total


def inspect_component_package(package, *, current_root=None, trusted_public_keys=None,
                              require_inventory=False, cancel=None):
    """Validate a ZIP without extracting it or touching a live tree."""
    package_path = _plain_path(Path(package), allow_missing=False)
    if not package_path.is_file():
        raise ComponentPackageError("AMPKG 文件不存在")
    archive, manifest, records = _read_zip_manifest(package_path)
    try:
        # Establish manifest authority before reading attacker-controlled
        # payload bytes.  This ordering also bounds work for a ZIP bomb.
        _verify_signature(manifest, trusted_public_keys)
        if current_root is not None:
            root = _plain_path(Path(current_root), allow_missing=False)
            catalog, by_id = _catalog_check(root, manifest)
            inventory = _read_inventory(root)
            authorized = {_key(item["path"]) for item in _authorized_component_records(
                catalog, manifest["component_ids"] + manifest["dependencies"], by_id, inventory)}
            _inventory_check(root, records, authorized_paths=authorized)
        elif require_inventory:
            raise ComponentPackageError("验证本地无签名包需要 current_root")
        total = _hash_zip_payload(archive, records, cancel)
        manifest_digest = _sha256_bytes(_json_bytes(manifest))
        return {
            "status": "VERIFIED",
            "package_path": str(package_path),
            "package_sha256": _sha256_file(package_path),
            "manifest_sha256": manifest_digest,
            "manifest": manifest,
            "files": records,
            "file_count": len(records),
            "payload_bytes": total,
            "signature_status": manifest["signature"]["status"],
            "inventory_status": "MATCHED" if current_root is not None else "NOT_CHECKED",
        }
    finally:
        archive.close()


def build_component_package(root, component_ids, output, *, signing_key=None,
                           package_id=None, version=None, app_compatibility=None,
                           cancel=None, progress=None):
    """Export managed files from the current inventory to a real ``.ampkg`` ZIP."""
    source_root = _plain_path(Path(root), allow_missing=False)
    if not source_root.is_dir():
        raise ComponentPackageError("组件源根目录不存在")
    catalog, selected, dependencies, by_id = _component_selection(source_root, component_ids)
    inventory = _read_inventory(source_root)
    records = _file_records(source_root, catalog, selected + dependencies, by_id, inventory, cancel)
    current_version = _version(source_root)
    selected_version = current_version if version is None else version
    if not isinstance(selected_version, str) or not selected_version or len(selected_version) > 256:
        raise ComponentPackageError("AMPKG version 无效")
    if selected_version != current_version:
        raise ComponentPackageError("AMPKG version 必须与当前 VERSION 一致")
    if package_id is None:
        package_id = "ampkg-" + uuid.uuid4().hex
    if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
        raise ComponentPackageError("AMPKG package_id 无效")
    compatibility = app_compatibility or {
        "app_version": selected_version,
        "catalog_schema_version": 1,
    }
    if not isinstance(compatibility, dict) or compatibility.get("catalog_schema_version") != 1:
        raise ComponentPackageError("app_compatibility 必须包含 catalog_schema_version=1")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package_kind": PACKAGE_KIND,
        "package_id": package_id,
        "version": selected_version,
        "component_ids": selected,
        "dependencies": dependencies,
        "app_compatibility": compatibility,
        "license_status": "UNKNOWN",
        "distribution_status": "UNKNOWN",
        "signature": {"status": UNSIGNED_STATUS},
        "files": records,
    }
    if signing_key is not None:
        manifest = _sign_manifest(manifest, signing_key)
    manifest_bytes = _json_bytes(manifest)
    destination = _plain_path(Path(output))
    if destination.exists() or os.path.lexists(str(destination)):
        raise ComponentPackageError("AMPKG 输出文件已存在：" + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    written = 0
    try:
        _checkpoint(cancel)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            manifest_info = zipfile.ZipInfo(MANIFEST_NAME, (1980, 1, 1, 0, 0, 0))
            manifest_info.compress_type = zipfile.ZIP_STORED
            manifest_info.external_attr = 0o100644 << 16
            archive.writestr(manifest_info, manifest_bytes)
            for index, record in enumerate(records, 1):
                _checkpoint(cancel)
                source = source_root / Path(*record["path"].split("/"))
                info = zipfile.ZipInfo(_zip_member_name(record["path"]), (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                digest = hashlib.sha256()
                size = 0
                with source.open("rb") as source_handle, archive.open(info, "w") as target_handle:
                    for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                        _checkpoint(cancel)
                        target_handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise ComponentPackageError("源文件在打包期间发生变化：" + record["path"])
                written += 1
                if progress:
                    progress({"event": "file", "index": index, "files": len(records),
                              "path": record["path"], "bytes": size})
        _checkpoint(cancel)
        os.replace(temporary, destination)
    except Exception:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        raise
    return {
        "status": "CREATED",
        "package_path": str(destination),
        "package_sha256": _sha256_file(destination),
        "manifest": manifest,
        "file_count": written,
        "payload_bytes": sum(item["size"] for item in records),
        "signature_status": manifest["signature"]["status"],
    }


def extract_component_package(package, destination=None, *, current_root=None,
                              user_temp_root=None, trusted_public_keys=None,
                              cancel=None):
    """Verify and extract to a caller-owned userdata temp directory.

    The returned directory has direct component paths and a direct
    ``manifest.json``, so it can be handed to ``install_offline_package``.
    """
    verification = inspect_component_package(
        package, current_root=current_root, trusted_public_keys=trusted_public_keys,
        require_inventory=current_root is None, cancel=cancel,
    )
    if user_temp_root is None:
        raise ComponentPackageError("AMPKG 解包必须指定 userdata-owned temp 根目录")
    temp_root = _plain_path(Path(user_temp_root))
    temp_root.mkdir(parents=True, exist_ok=True)
    if not temp_root.is_dir():
        raise ComponentPackageError("userdata temp 根目录无效")
    payload_bytes = verification["payload_bytes"]
    required_free = payload_bytes + max(16 * 1024 * 1024, payload_bytes // 100)
    try:
        if shutil.disk_usage(str(temp_root)).free < required_free:
            raise ComponentPackageError("userdata temp 所在磁盘可用空间不足")
    except OSError as exc:
        raise ComponentPackageError("无法检查 userdata temp 磁盘空间") from exc
    if destination is None:
        destination_path = temp_root / ("ampkg-" + uuid.uuid4().hex)
        owns_destination = True
    else:
        destination_path = _plain_path(Path(destination))
        owns_destination = not destination_path.exists()
        try:
            if os.path.commonpath((os.path.normcase(str(temp_root)),
                                   os.path.normcase(str(destination_path)))) != os.path.normcase(str(temp_root)):
                raise ComponentPackageError("AMPKG 解包目标必须位于 userdata-owned temp 下")
        except ComponentPackageError:
            raise
        except ValueError as exc:
            raise ComponentPackageError("AMPKG 解包目标跨越不同路径根") from exc
    if destination_path.exists() and any(destination_path.iterdir()):
        raise ComponentPackageError("AMPKG 解包目标必须为空")
    destination_path.mkdir(parents=True, exist_ok=True)
    created = []
    package_path = _plain_path(Path(package), allow_missing=False)
    archive, manifest, records = _read_zip_manifest(package_path)
    try:
        # The first verification closes its ZIP before extraction.  Re-opened
        # input must be proven to be the same bytes and manifest, and all
        # current-root authority checks must run again before any payload is
        # read.  This closes the common atomic-replace TOCTOU window.
        if _sha256_file(package_path) != verification["package_sha256"]:
            raise ComponentPackageError("AMPKG 在验证与解包之间发生替换")
        if _sha256_bytes(_json_bytes(manifest)) != verification["manifest_sha256"]:
            raise ComponentPackageError("AMPKG manifest 在验证与解包之间发生替换")
        _verify_signature(manifest, trusted_public_keys)
        if current_root is not None:
            root = _plain_path(Path(current_root), allow_missing=False)
            catalog, by_id = _catalog_check(root, manifest)
            inventory = _read_inventory(root)
            authorized = {_key(item["path"]) for item in _authorized_component_records(
                catalog, manifest["component_ids"] + manifest["dependencies"], by_id, inventory)}
            _inventory_check(root, records, authorized_paths=authorized)
        else:
            raise ComponentPackageError("解包本地 AMPKG 必须提供 current_root")
        _hash_zip_payload(archive, records, cancel)
        for index, record in enumerate(records, 1):
            _checkpoint(cancel)
            relative_path = Path(*record["path"].split("/"))
            target = _plain_path(destination_path / relative_path)
            if destination_path not in target.parents:
                raise ComponentPackageError("AMPKG 解包路径越界")
            source_info = archive.getinfo(_zip_member_name(record["path"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            _plain_path(target.parent)
            temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
            digest = hashlib.sha256()
            size = 0
            try:
                with archive.open(source_info, "r") as source_handle, temporary.open("wb") as target_handle:
                    for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                        _checkpoint(cancel)
                        target_handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise ComponentPackageError("AMPKG 解包校验失败：" + record["path"])
                os.replace(temporary, target)
                created.append(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        _checkpoint(cancel)
        if _sha256_file(package_path) != verification["package_sha256"]:
            raise ComponentPackageError("AMPKG 在解包过程中发生替换")
        manifest_path = destination_path / MANIFEST_NAME
        _plain_path(manifest_path)
        manifest_path.write_bytes(_json_bytes(manifest))
        created.append(manifest_path)
        receipt = {
            "schema_version": 1,
            "package_kind": PACKAGE_KIND,
            "package_id": manifest["package_id"],
            "package_sha256": verification["package_sha256"],
            "manifest_sha256": _sha256_bytes(_json_bytes(manifest)),
            "signature_status": manifest["signature"]["status"],
            "inventory_status": verification["inventory_status"],
            "file_count": len(records),
            "payload_bytes": payload_bytes,
        }
        receipt_path = _plain_path(destination_path / RECEIPT_NAME)
        receipt_path.write_bytes(_json_bytes(receipt))
        created.append(receipt_path)
        return {
            "status": "EXTRACTED",
            "folder": str(destination_path),
            "manifest": manifest,
            "receipt": receipt,
            "receipt_path": str(receipt_path),
            "package_sha256": verification["package_sha256"],
        }
    except Exception:
        if owns_destination:
            shutil.rmtree(destination_path, ignore_errors=True)
        else:
            for path in reversed(created):
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                except OSError:
                    pass
        raise
    finally:
        archive.close()


# Names used by callers that prefer the verbs in install_platform.
create_component_package = build_component_package
read_component_package = extract_component_package
validate_component_package = inspect_component_package
