"""Safe installation and external-editor location helpers.

The component installer handles an immutable Portable bundle only.  It does
not merge files into an existing application tree: the source bundle is
verified in place, copied to a private staging directory, verified again, and
then published with one directory rename.  User data therefore never becomes
part of an installation transaction.

External editor paths are an explicit user setting.  They are validated as
ordinary Windows PE executables and persisted in a small, bounded JSON file;
the component registry and managed RVC/UVR paths are deliberately outside of
this API.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

from core.paths import PORTABLE_RELEASE_CONTENT, PORTABLE_RELEASE_MARKER, RELEASE_MANIFEST


EDITOR_KINDS = ("audacity", "musescore", "lmms", "neuralnote")
EDITOR_SETTINGS_NAME = "external-editors.json"
EDITOR_SETTINGS_VERSION = 1
MAX_SETTINGS_BYTES = 64 * 1024
MAX_EDITOR_PATH_CHARS = 32767
MAX_EDITOR_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_RECORDS = 1_000_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *("COM%d" % index for index in range(1, 10)),
    *("LPT%d" % index for index in range(1, 10)),
}
_MARKER_BYTES = (
    PORTABLE_RELEASE_CONTENT.encode("ascii"),
    (PORTABLE_RELEASE_CONTENT + "\n").encode("ascii"),
    (PORTABLE_RELEASE_CONTENT + "\r\n").encode("ascii"),
)


class ComponentLocationError(ValueError):
    """Raised when an installation source or editor setting is unsafe."""


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def _plain_ancestors(path, *, allow_missing_leaf=True) -> Path:
    """Return an absolute path after rejecting existing reparse ancestors."""
    raw = os.path.abspath(os.fspath(path))
    candidate = Path(raw)
    current = candidate if (candidate.exists() or not allow_missing_leaf) else candidate.parent
    while True:
        if _is_reparse(current):
            raise ComponentLocationError("路径包含符号链接或 junction：" + str(current))
        parent = current.parent
        if parent == current:
            break
        current = parent
    return candidate


def _assert_plain_tree(root: Path) -> None:
    if _is_reparse(root) or not root.is_dir():
        raise ComponentLocationError("Portable 源必须是普通目录：" + str(root))
    stack = [root]
    while stack:
        current = stack.pop()
        if _is_reparse(current):
            raise ComponentLocationError("Portable 源包含 reparse point：" + str(current))
        with os.scandir(current) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_reparse(child):
                    raise ComponentLocationError("Portable 源包含 reparse point：" + str(child))
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
                elif not entry.is_file(follow_symlinks=False):
                    raise ComponentLocationError("Portable 源包含特殊文件：" + str(child))


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ComponentLocationError("清单包含非法相对路径")
    parsed = PurePosixPath(value)
    parts = value.split("/")
    if (parsed.is_absolute() or re.match(r"^[A-Za-z]:", value)
            or any(part in ("", ".", "..") for part in parts)):
        raise ComponentLocationError("清单路径越界：" + value)
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if (not part or part.endswith((".", " ")) or stem in _WINDOWS_DEVICES
                or any(character in '<>:\\"|?*' or ord(character) < 32 for character in part)):
            raise ComponentLocationError("清单路径含 Windows 保留或非法名称：" + value)
    return value


def _json_digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_or_nested(left: Path, right: Path) -> bool:
    """Whether two paths are equal or one is beneath the other."""
    left_text = os.path.normcase(os.path.abspath(os.fspath(left))).rstrip("\\/")
    right_text = os.path.normcase(os.path.abspath(os.fspath(right))).rstrip("\\/")
    try:
        return os.path.commonpath((left_text, right_text)) in (left_text, right_text)
    except ValueError:
        return False


def _read_manifest(source: Path):
    marker = source / PORTABLE_RELEASE_MARKER
    if _is_reparse(marker) or not marker.is_file() or marker.read_bytes() not in _MARKER_BYTES:
        raise ComponentLocationError("缺少有效 PORTABLE_RELEASE 标记")
    manifest_path = source / RELEASE_MANIFEST
    if _is_reparse(manifest_path) or not manifest_path.is_file():
        raise ComponentLocationError("缺少有效 release-manifest.json")
    try:
        if manifest_path.stat().st_size > MAX_SETTINGS_BYTES:
            # A release manifest is normally much larger than editor settings;
            # permit a package-sized manifest but cap pathological JSON input.
            if manifest_path.stat().st_size > 256 * 1024 * 1024:
                raise ComponentLocationError("release manifest 过大")
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComponentLocationError("release manifest 无法读取") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ComponentLocationError("release manifest schema 无效")
    records = document.get("files")
    if (not isinstance(records, list) or not records or len(records) > MAX_MANIFEST_RECORDS
            or document.get("file_count") != len(records)):
        raise ComponentLocationError("release manifest 文件清单不完整")
    if document.get("file_inventory_sha256") != _json_digest(records):
        raise ComponentLocationError("release manifest 文件清单摘要不匹配")
    normalized = []
    seen = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise ComponentLocationError("release manifest 文件记录不完整")
        relative = _safe_relative(record["path"])
        key = relative.casefold()
        size, digest = record["size"], record["sha256"]
        if (key in seen or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            raise ComponentLocationError("release manifest 文件记录重复或摘要无效")
        if key == RELEASE_MANIFEST.casefold():
            raise ComponentLocationError("release manifest 不得自包含自身")
        # Mutable application data is never part of a Portable bundle install.
        first = key.split("/", 1)[0]
        if first in {"config", "output", "temp", "logs", "song", "models", "处理后", "userdata", "user-data"}:
            raise ComponentLocationError("release manifest 试图迁移用户数据：" + relative)
        seen.add(key)
        normalized.append({"path": relative, "size": size, "sha256": digest})
    return document, normalized


def _validate_source_files(source: Path, records) -> int:
    expected = {record["path"].casefold(): record for record in records}
    actual = {}
    total = 0
    for path in source.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(source).as_posix()
        if relative.casefold() == RELEASE_MANIFEST.casefold():
            continue
        if _is_reparse(path):
            raise ComponentLocationError("Portable 源包含 reparse point：" + str(path))
        key = relative.casefold()
        if key in actual:
            raise ComponentLocationError("Portable 源文件名大小写冲突：" + relative)
        if key == PORTABLE_RELEASE_MARKER.casefold() and key not in expected:
            # Older local fixtures did not include the marker in the builder
            # inventory.  The marker itself was still authenticated above and
            # will be copied byte-for-byte during installation.
            continue
        actual[key] = relative
        record = expected.get(key)
        if record is None:
            raise ComponentLocationError("Portable 源包含未列入清单的文件：" + relative)
        size = path.stat().st_size
        if size != record["size"] or _sha256(path) != record["sha256"]:
            raise ComponentLocationError("Portable 源文件校验失败：" + relative)
        total += size
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        raise ComponentLocationError("Portable 源缺少清单文件：" + (missing[0] if missing else "未知"))
    return total


def _copy_verified(source: Path, target: Path, record) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(target.parent):
        raise ComponentLocationError("安装 staging 包含 reparse point：" + str(target.parent))
    shutil.copyfile(source, target)
    stat_result = target.stat()
    if stat_result.st_size != record["size"] or _sha256(target) != record["sha256"]:
        raise ComponentLocationError("staging 文件校验失败：" + record["path"])


def install_verified_bundle(source_root, target_root, progress=None):
    """Install a complete, hash-pinned Portable bundle into a new directory.

    ``source_root`` is never modified.  ``target_root`` must not exist before
    the call, and the final directory is published only after every listed file
    has been copied and hashed in private staging.  ``progress`` receives a
    bounded dictionary for each file and one final ``complete`` event.
    """
    source = _plain_ancestors(source_root)
    target = _plain_ancestors(target_root)
    if not source.is_dir():
        raise ComponentLocationError("Portable 源目录不存在：" + str(source))
    if target.exists() or os.path.lexists(str(target)):
        raise ComponentLocationError("安装目标必须是全新目录：" + str(target))
    if _same_or_nested(source, target):
        raise ComponentLocationError("安装目标不能位于源目录内或与源目录相同")
    if _same_or_nested(target, source):
        raise ComponentLocationError("源目录不能位于安装目标内")
    parent = target.parent
    _plain_ancestors(parent)
    if parent.exists() and _is_reparse(parent):
        raise ComponentLocationError("安装目标父目录包含 reparse point")
    # The selected bundle root may be a new nested directory.  Creating only
    # its parent chain is safe because the target itself is still required to
    # be absent and all bundle bytes remain in the private staging directory.
    parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(parent):
        raise ComponentLocationError("安装目标父目录包含 reparse point")

    _assert_plain_tree(source)
    document, records = _read_manifest(source)
    total_bytes = _validate_source_files(source, records)
    staging = parent / ("." + target.name + "." + uuid.uuid4().hex + ".installing")
    if staging.exists() or os.path.lexists(str(staging)):
        raise ComponentLocationError("staging 路径已存在")
    copied = 0
    copied_bytes = 0
    try:
        staging.mkdir(parents=False, exist_ok=False)
        if progress:
            progress({"event": "start", "files": len(records), "bytes": total_bytes})
        for record in records:
            relative = Path(*record["path"].split("/"))
            _copy_verified(source / relative, staging / relative, record)
            copied += 1
            copied_bytes += record["size"]
            if progress:
                progress({"event": "file", "path": record["path"], "index": copied,
                          "files": len(records), "bytes": copied_bytes, "total_bytes": total_bytes})
        expected_paths = {record["path"].casefold() for record in records}
        marker_source = source / PORTABLE_RELEASE_MARKER
        marker_target = staging / PORTABLE_RELEASE_MARKER
        if PORTABLE_RELEASE_MARKER.casefold() not in expected_paths:
            # Legacy manifests may omit the marker.  Preserve its authenticated
            # original bytes, including a standard trailing newline.
            shutil.copyfile(marker_source, marker_target)
        manifest_target = staging / RELEASE_MANIFEST
        shutil.copyfile(source / RELEASE_MANIFEST, manifest_target)
        if _sha256(manifest_target) != _sha256(source / RELEASE_MANIFEST):
            raise ComponentLocationError("release manifest staging 校验失败")
        # Recheck the staging tree before the one directory rename.
        for path in staging.rglob("*"):
            if path.is_dir():
                continue
            if _is_reparse(path):
                raise ComponentLocationError("staging 包含 reparse point：" + str(path))
        if target.exists() or os.path.lexists(str(target)):
            raise ComponentLocationError("安装目标在提交前已存在")
        os.replace(str(staging), str(target))
    except Exception:
        if staging.exists() or os.path.lexists(str(staging)):
            cleanup_error = None
            try:
                stage_absolute = Path(os.path.abspath(os.fspath(staging)))
                parent_absolute = Path(os.path.abspath(os.fspath(parent)))
                prefix = "." + target.name + "."
                token = stage_absolute.name[len(prefix):-len(".installing")] if (
                    stage_absolute.name.startswith(prefix) and stage_absolute.name.endswith(".installing")
                ) else ""
                if (stage_absolute.parent != parent_absolute
                        or not re.fullmatch(r"[0-9a-f]{32}", token)):
                    raise ComponentLocationError("拒绝清理非本次事务的 staging 路径")
                _plain_ancestors(parent_absolute, allow_missing_leaf=False)
                if _is_reparse(stage_absolute):
                    raise ComponentLocationError("拒绝递归删除包含 reparse point 的 staging")
                _assert_plain_tree(stage_absolute)
                shutil.rmtree(stage_absolute)
            except Exception as exc:
                cleanup_error = exc
            if cleanup_error is not None:
                raise ComponentLocationError("staging 清理失败，已停止以保留现场：" + str(cleanup_error)) from cleanup_error
        raise
    if progress:
        progress({"event": "complete", "files": copied, "bytes": copied_bytes,
                  "target_root": str(target)})
    return {
        "status": "INSTALLED",
        "source_root": str(source),
        "target_root": str(target),
        "files": copied,
        "bytes": copied_bytes,
        "manifest_sha256": document.get("file_inventory_sha256"),
        "source_provenance": document.get("source"),
        "user_data_copied": False,
        "atomic": True,
    }


def _validate_editor_kind(kind: str) -> str:
    if not isinstance(kind, str) or kind not in EDITOR_KINDS:
        raise ComponentLocationError("不支持的外部编辑器类型：" + str(kind))
    return kind


def _editor_path(path) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        raise ComponentLocationError("编辑器路径必须是绝对 exe 文件")
    text = os.fspath(path)
    if not isinstance(text, str) or len(text) > MAX_EDITOR_PATH_CHARS or not os.path.isabs(text):
        raise ComponentLocationError("编辑器路径必须是绝对路径")
    normalized = text.replace("/", "\\")
    parts = normalized.split("\\")
    # Permit only the colon in a drive prefix; this rejects ADS streams and
    # drive-relative names consistently on Windows and in cross-platform tests.
    for index, part in enumerate(parts):
        if index == 0 and re.fullmatch(r"[A-Za-z]:", part):
            continue
        if index < 2 and not part:  # leading separators in an UNC path
            continue
        if ":" in part or any(character in '<>"|?*\x00' or ord(character) < 32 for character in part):
            raise ComponentLocationError("编辑器路径含非法字符或 ADS")
        if part.rstrip(" .").upper() in _WINDOWS_DEVICES:
            raise ComponentLocationError("编辑器路径使用 Windows 保留设备名")
    if not Path(text).suffix.casefold() == ".exe":
        raise ComponentLocationError("外部编辑器必须是 .exe")
    candidate = _plain_ancestors(text)
    if _is_reparse(candidate) or not candidate.is_file():
        raise ComponentLocationError("外部编辑器路径不存在或包含 reparse point")
    info = candidate.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > MAX_EDITOR_BYTES:
        raise ComponentLocationError("外部编辑器文件大小不在安全范围")
    with candidate.open("rb") as handle:
        header = handle.read(4096)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ComponentLocationError("外部编辑器不是有效 PE 文件")
        pe_offset = int.from_bytes(header[0x3C:0x40], "little")
        if pe_offset < 64 or pe_offset + 4 > 4096 or header[pe_offset:pe_offset + 4] != b"PE\x00\x00":
            raise ComponentLocationError("外部编辑器 PE 头无效")
    return candidate


def _settings_path(config_dir) -> Path:
    directory = _plain_ancestors(config_dir)
    if directory.exists() and not directory.is_dir():
        raise ComponentLocationError("编辑器设置目录不是目录")
    return directory / EDITOR_SETTINGS_NAME


def _read_settings(config_dir):
    path = _settings_path(config_dir)
    if not path.exists():
        return {}, path
    if _is_reparse(path) or path.stat().st_size > MAX_SETTINGS_BYTES:
        raise ComponentLocationError("编辑器设置文件损坏或过大")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComponentLocationError("编辑器设置 JSON 损坏") from exc
    if not isinstance(payload, dict):
        raise ComponentLocationError("编辑器设置 JSON 必须是对象")
    version = payload.get("schema_version", EDITOR_SETTINGS_VERSION)
    if version != EDITOR_SETTINGS_VERSION:
        raise ComponentLocationError("编辑器设置版本不支持")
    editors = payload.get("editors", {})
    if not isinstance(editors, dict):
        raise ComponentLocationError("编辑器设置 editors 字段无效")
    for kind, metadata in editors.items():
        if kind not in EDITOR_KINDS or not isinstance(metadata, dict):
            raise ComponentLocationError("编辑器设置条目无效")
        if len(json.dumps(metadata, ensure_ascii=False)) > 8192:
            raise ComponentLocationError("编辑器设置元数据过大")
    return payload, path


def save_editor_location(config_dir, kind, path):
    """Persist one explicitly selected external editor without losing settings."""
    kind = _validate_editor_kind(kind)
    editor = _editor_path(path)
    payload, settings_path = _read_settings(config_dir)
    payload.setdefault("schema_version", EDITOR_SETTINGS_VERSION)
    editors = payload.setdefault("editors", {})
    info = editor.stat()
    editors[kind] = {
        "path": str(editor),
        "size": info.st_size,
        "sha256": _sha256(editor),
        "validated_at_utc": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_SETTINGS_BYTES:
        raise ComponentLocationError("编辑器设置文件过大")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(settings_path.parent):
        raise ComponentLocationError("编辑器设置目录包含 reparse point")
    temporary = settings_path.with_name("." + settings_path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(str(temporary), str(settings_path))
    finally:
        if temporary.exists() or os.path.lexists(str(temporary)):
            temporary.unlink(missing_ok=True)
    return {"kind": kind, "path": str(editor), "size": info.st_size, "sha256": _sha256(editor)}


def resolve_editor_location(config_dir, kind, default):
    """Return a saved validated path, or ``default`` when no path is saved.

    This validates executable identity and stored metadata only.  It does not
    launch the editor or claim that MIDI/audio import succeeded.
    """
    kind = _validate_editor_kind(kind)
    payload, _settings_path_value = _read_settings(config_dir)
    metadata = payload.get("editors", {}).get(kind)
    if metadata is None:
        return default
    required = {"path", "size", "sha256", "validated_at_utc"}
    if not required.issubset(metadata):
        raise ComponentLocationError("编辑器设置元数据不完整")
    if (isinstance(metadata["size"], bool) or not isinstance(metadata["size"], int)
            or metadata["size"] <= 0 or metadata["size"] > MAX_EDITOR_BYTES
            or not isinstance(metadata["sha256"], str) or not _SHA256.fullmatch(metadata["sha256"])
            or not isinstance(metadata["validated_at_utc"], str) or len(metadata["validated_at_utc"]) > 64):
        raise ComponentLocationError("编辑器设置元数据无效")
    editor = _editor_path(metadata["path"])
    info = editor.stat()
    if info.st_size != metadata["size"] or _sha256(editor) != metadata["sha256"]:
        raise ComponentLocationError("已保存的编辑器文件已改变")
    return str(editor)


__all__ = [
    "EDITOR_KINDS", "EDITOR_SETTINGS_NAME", "ComponentLocationError",
    "install_verified_bundle", "save_editor_location", "resolve_editor_location",
]
