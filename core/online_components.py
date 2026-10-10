"""Small-distribution component planning and verified acquisition.

The source distribution deliberately does not contain the bundled Python,
Torch, FFmpeg, vendor trees, or model weights.  This module is the narrow
bridge used by the native PowerShell bootstrap: it validates a checked-in
source manifest, downloads only hash-pinned artifacts, stages verified
archives and wheels, and writes a separate modular Polish runtime manifest.

An entry without an authoritative pinned artifact is reported as unavailable
or user-owned. It is never treated as a successful download. Strict full
runtime manifests are preserved; lightweight installs are Polish-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
import zipfile
from pathlib import Path

# The native bootstrap invokes this file by absolute path before a package
# installation exists; make the source root importable in that mode.
if __package__ in (None, ""):
    _SOURCE_ROOT = str(Path(__file__).resolve().parents[1])
    if _SOURCE_ROOT not in sys.path:
        sys.path.insert(0, _SOURCE_ROOT)

from core.component_platform import dependency_closure, load_catalog
from core.download_manager import download_verified
from core.install_platform import install_offline_package, read_offline_package

SCHEMA_VERSION = 1
SHA256_LENGTH = 64
SOURCE_STATUSES = {"PINNED", "DERIVED", "USER_OWNED_ONLY", "UNAVAILABLE"}
INSTALLABLE_KINDS = {"archive", "wheel", "python_installer", "ampkg"}
FORBIDDEN_ROOTS = {"config", "output", "temp", "logs", "song", "models", "处理后"}
POLISH_BOOTSTRAP_IDS = ("python.numpy", "python.scipy", "python.soundfile", "python.cffi", "python.pycparser", "python.mido", "python.packaging")
SOURCE_STATUS_DESCRIPTIONS = {
    "PINNED": "可官方在线下载（SHA-256已锁定）",
    "DERIVED": "随源码或已安装依赖提供（无需单独下载）",
    "UNAVAILABLE": "缺少可信公开来源（需同版本离线包导入）",
    "USER_OWNED_ONLY": "用户自有资产（需自行导入权重文件）",
}


class OnlineComponentError(ValueError):
    """The lightweight source manifest or an acquisition is unsafe/incomplete."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _progress(callback, event, **fields):
    if callback:
        payload = {"event": event, **fields}
        callback(payload)


def _safe_relative(relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise OnlineComponentError("非法相对路径")
    path = Path(relative)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise OnlineComponentError("路径越界：" + str(relative))
    if path.parts[0].casefold() in {item.casefold() for item in FORBIDDEN_ROOTS}:
        raise OnlineComponentError("安装包不能写用户数据目录：" + relative)
    return "/".join(path.parts)


def _allowed_url(url, hosts, *, allow_http_loopback=False):
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.username or parsed.password or parsed.fragment:
        raise OnlineComponentError("下载地址不能含凭据或 fragment")
    if parsed.hostname is None or parsed.hostname.casefold() not in {str(item).casefold() for item in hosts}:
        raise OnlineComponentError("下载地址不在显式主机白名单中")
    if parsed.scheme != "https" and not (allow_http_loopback and local and parsed.scheme == "http"):
        raise OnlineComponentError("下载地址必须使用 HTTPS")


def _validate_pinned_entry(entry, *, label):
    """Validate one hash-pinned downloadable record from either manifest list."""
    component_id = entry.get("component_id") or label
    kind = entry.get("kind")
    digest = entry.get("sha256")
    urls = entry.get("urls")
    hosts = entry.get("allowed_hosts")
    if kind not in INSTALLABLE_KINDS or not isinstance(urls, list) or not urls:
        raise OnlineComponentError("PINNED 来源缺少 artifact kind/urls：" + component_id)
    if not isinstance(digest, str) or len(digest) != SHA256_LENGTH or digest != digest.lower() or any(char not in "0123456789abcdef" for char in digest):
        raise OnlineComponentError("PINNED 来源必须有小写 SHA-256：" + component_id)
    if not isinstance(hosts, list) or not hosts or any(not isinstance(item, str) or not item for item in hosts):
        raise OnlineComponentError("PINNED 来源缺少 allowed_hosts：" + component_id)
    for url in urls:
        if not isinstance(url, str):
            raise OnlineComponentError("来源 URL 无效：" + component_id)
        _allowed_url(url, hosts)
    size = entry.get("size_bytes")
    if type(size) is not int or size <= 0:
        raise OnlineComponentError("PINNED 来源必须有 size_bytes：" + component_id)


def load_sources(path, *, catalog=None, version=None):
    """Load and validate ``packaging/download-sources.json``.

    Every catalog component needs exactly one declaration.  Only ``PINNED``
    entries may contain URLs; those entries must carry a lowercase SHA-256 and
    an explicit HTTPS host allow-list.
    """
    path = Path(path).resolve(strict=True)
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OnlineComponentError("下载来源清单无法读取") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise OnlineComponentError("下载来源清单 schema_version 无效")
    app_version = document.get("app_version")
    if not isinstance(app_version, str) or not app_version:
        raise OnlineComponentError("下载来源清单缺少 app_version")
    if version is not None and app_version != version:
        raise OnlineComponentError("下载来源清单版本与源码 VERSION 不同")
    policy = document.get("policy")
    if not isinstance(policy, dict) or policy.get("transport") != "https-only-with-explicit-hosts":
        raise OnlineComponentError("下载来源清单缺少 HTTPS 主机策略")
    entries = document.get("sources")
    if not isinstance(entries, list) or not entries:
        raise OnlineComponentError("下载来源清单缺少 sources")
    by_id = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("component_id"), str):
            raise OnlineComponentError("下载来源记录无效")
        component_id = entry["component_id"]
        if component_id in by_id:
            raise OnlineComponentError("重复来源：" + component_id)
        status = entry.get("status")
        if status not in SOURCE_STATUSES:
            raise OnlineComponentError("来源状态无效：" + component_id)
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise OnlineComponentError("来源必须有可审计 reason：" + component_id)
        if status == "PINNED":
            _validate_pinned_entry(entry, label=component_id)
        else:
            for key in ("urls", "allowed_hosts", "sha256", "size_bytes"):
                if key in entry and entry[key] not in (None, [], ""):
                    raise OnlineComponentError("非 PINNED 来源不能伪装下载资产：" + component_id)
        by_id[component_id] = entry
    if catalog is not None:
        catalog_ids = {item["id"] for item in catalog.get("components", [])}
        if set(by_id) != catalog_ids:
            missing = sorted(catalog_ids - set(by_id))
            extra = sorted(set(by_id) - catalog_ids)
            raise OnlineComponentError("来源覆盖与组件目录不一致；missing=%s extra=%s" % (missing, extra))
    return document, by_id


def load_bootstrap_packages(document):
    """Validate and return optional Python wheels outside the component catalog."""
    entries = document.get("bootstrap_packages", [])
    if not isinstance(entries, list):
        raise OnlineComponentError("bootstrap_packages 必须是数组")
    by_id = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("component_id"), str):
            raise OnlineComponentError("bootstrap package 记录无效")
        component_id = entry["component_id"]
        if component_id in by_id:
            raise OnlineComponentError("重复 bootstrap package：" + component_id)
        if entry.get("status") != "PINNED":
            raise OnlineComponentError("bootstrap package 必须是 PINNED：" + component_id)
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise OnlineComponentError("bootstrap package 缺少 reason：" + component_id)
        _validate_pinned_entry(entry, label=component_id)
        by_id[component_id] = entry
    return by_id


def _component_ids(root, requested):
    catalog = load_catalog(Path(root) / "runtime-manifest" / "components.json")
    by_id = {item["id"]: item for item in catalog["components"]}
    if requested in (None, "", [], ["all"], "all"):
        requested = [item["id"] for item in catalog["components"] if item["required"] and item["implemented"]]
    if isinstance(requested, str):
        requested = [item.strip() for item in requested.split(",") if item.strip()]
    if not isinstance(requested, (list, tuple)) or not requested:
        raise OnlineComponentError("至少选择一个组件或模式")
    selected = []
    mode_map = {"polish": "mode.polish", "voice_conversion": "mode.voice_conversion", "transcribe": "mode.transcribe"}
    for raw in requested:
        component_id = mode_map.get(raw, raw)
        if component_id not in by_id:
            raise OnlineComponentError("未知组件或模式：" + str(raw))
        if component_id not in selected:
            selected.append(component_id)
    resolved = []
    for component_id in selected:
        item = by_id[component_id]
        if not item["implemented"]:
            raise OnlineComponentError("组件未实现：" + component_id)
        for dependency in dependency_closure(catalog, component_id):
            if dependency not in resolved and dependency not in selected:
                resolved.append(dependency)
    return catalog, selected, resolved


def plan(root, source_manifest, requested=None):
    root = Path(root).resolve(strict=True)
    version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    catalog, selected, dependencies = _component_ids(root, requested)
    document, sources = load_sources(source_manifest, catalog=catalog, version=version)
    bootstrap = load_bootstrap_packages(document)
    component_ids = selected + dependencies
    rows = []
    for component_id in component_ids:
        entry = sources[component_id]
        status = entry["status"]
        rows.append({
            "component_id": component_id,
            "status": status,
            "human_status": SOURCE_STATUS_DESCRIPTIONS.get(status, status),
            "kind": entry.get("kind"),
            "version": entry.get("version"),
            "reason": entry["reason"],
            "downloadable": status == "PINNED",
            "install_strategy": entry.get("install_strategy"),
            "source_kind": "component",
        })
    wants_polish = "mode.polish" in component_ids or "app.core" in component_ids and requested in (None, "", [], ["all"], "all")
    bootstrap_ids = list(POLISH_BOOTSTRAP_IDS) if wants_polish else []
    for component_id in component_ids:
        for dependency_id in sources[component_id].get("bootstrap_dependencies", []):
            if dependency_id not in bootstrap_ids:
                bootstrap_ids.append(dependency_id)
    for package_id in bootstrap_ids:
        entry = bootstrap.get(package_id)
        if entry is None:
            raise OnlineComponentError("缺少 Polish bootstrap package：" + package_id)
        rows.append({
            "component_id": package_id,
            "status": entry["status"],
            "human_status": SOURCE_STATUS_DESCRIPTIONS.get(entry["status"], entry["status"]),
            "kind": entry.get("kind"),
            "version": entry.get("version"),
            "reason": entry["reason"],
            "downloadable": True,
            "install_strategy": entry.get("install_strategy"),
            "source_kind": "bootstrap",
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "app_version": version,
        "selected": selected,
        "dependencies": dependencies,
        "component_ids": component_ids,
        "bootstrap_packages": bootstrap_ids,
        "rows": rows,
        "all_sources_authoritative": True,
        "policy": document["policy"],
    }


def _safe_zip_extract(archive_path, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        members = archive.infolist()
        if len(members) > 200000:
            raise OnlineComponentError("完整离线包文件数超过限制")
        for info in members:
            if info.is_dir():
                continue
            if "\\" in info.filename or "\x00" in info.filename:
                raise OnlineComponentError("完整离线包含非法成员")
            relative = _safe_relative(info.filename)
            target = (destination / Path(*relative.split("/"))).resolve()
            if destination not in target.parents:
                raise OnlineComponentError("完整离线包路径越界")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _write_bootstrap_receipt(root, package_path, manifest, installed, *, status):
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "source": str(package_path),
        "source_sha256": _sha256(package_path) if Path(package_path).is_file() else None,
        "app_version": (root / "VERSION").read_text(encoding="utf-8-sig").strip(),
        "manifest_version": manifest.get("version"),
        "installed_files": sorted(str(Path(item).relative_to(root)).replace("\\", "/") for item in installed),
        "user_data_preserved": True,
    }
    target = root / "runtime-manifest" / "lightweight-install.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(_json_bytes(receipt))
    os.replace(temporary, target)
    return receipt


def _verify_complete_donor(root, progress=None):
    """Run the strict release verifier before reporting a donor as installed."""
    from core.runtime_verify import verify_runtime

    data_root = os.environ.get("AI_MUSIC_PROCESSOR_DATA_ROOT")
    if data_root:
        data_root = Path(data_root).expanduser().resolve()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data).expanduser() if local_app_data else Path(root).parent
        data_root = (base / "AI-Music-Processor").resolve()
    result = verify_runtime(
        root=str(root), data_root=str(data_root), timeout=900, fast=False,
        progress=(lambda message: _progress(progress, "verify", message=message)) if progress else None,
    )
    if result.get("status") not in {"PASS", "PASS_WITH_WARNINGS"}:
        raise OnlineComponentError("完整 donor 严格运行时验收失败：" + str(result.get("error") or result.get("status")))
    return result


def _install_complete_donor(package, root, progress=None):
    """Install a full donor into a lean checkout, preserving user directories."""
    package = Path(package).resolve(strict=True)
    root = Path(root).resolve(strict=True)
    if package.suffix.casefold() == ".ampkg":
        inventory = root / "runtime-manifest" / "inventory.json"
        if not inventory.is_file():
            raise OnlineComponentError("当前轻量源码尚无可信 inventory；请先安装同版本完整 donor，再导入 .ampkg")
        try:
            result = install_offline_package(str(package), str(root))
        except Exception as exc:
            raise OnlineComponentError(".ampkg 原子安装失败：" + str(exc)) from exc
        _progress(progress, "complete", status=result.get("status", "INSTALLED"), files=len(result.get("installed", [])))
        return result
    temporary = Path(tempfile.mkdtemp(prefix="amp-light-donor-", dir=str(root.parent)))
    donor = temporary / "donor"
    installed = []
    try:
        if package.is_dir():
            donor = package
        elif package.suffix.casefold() == ".zip":
            _safe_zip_extract(package, donor)
        else:
            raise OnlineComponentError("离线源必须是完整目录或 ZIP")
        manifest = read_offline_package(str(donor))
        expected_version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
        donor_version = manifest.get("version") or manifest.get("app_version")
        if donor_version and donor_version != expected_version:
            raise OnlineComponentError("完整离线包 VERSION 不匹配")
        staging = Path(tempfile.mkdtemp(prefix="amp-light-stage-", dir=str(root.parent)))
        backup_root = Path(tempfile.mkdtemp(prefix="amp-light-backups-", dir=str(root.parent)))
        keep_backup = False
        backups = []
        donor_paths = {str(item["path"]).replace("\\", "/").casefold() for item in manifest["files"]}
        try:
            for index, item in enumerate(manifest["files"], 1):
                relative = _safe_relative(item["path"])
                source = donor / Path(*relative.split("/"))
                if not source.is_file() or _sha256(source) != item["sha256"]:
                    raise OnlineComponentError("完整离线包文件校验失败：" + relative)
                target = root / Path(*relative.split("/"))
                staged = staging / Path(*relative.split("/"))
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, staged)
                if _sha256(staged) != item["sha256"]:
                    raise OnlineComponentError("暂存文件校验失败：" + relative)
                _progress(progress, "stage", index=index, files=len(manifest["files"]), path=relative)
            # Remove stale managed runtime files from a previous lightweight
            # install.  User-owned models/output/config are outside these
            # roots and are preserved.
            cleanup_roots = (
                root / "runtime",
                root / "tools" / "RVC" / "RVC-beta-v2-0618" / "runtime",
                root / "runtime-manifest",
            )
            for cleanup_root in cleanup_roots:
                if not cleanup_root.is_dir():
                    continue
                for existing in sorted(cleanup_root.rglob("*")):
                    if not existing.is_file() or existing.is_symlink():
                        continue
                    relative = existing.relative_to(root).as_posix()
                    if relative.casefold() in donor_paths:
                        continue
                    backup = backup_root / ("amp-light-backup-" + uuid.uuid4().hex)
                    shutil.copyfile(existing, backup)
                    existing.unlink()
                    backups.append((existing, backup))
            for staged in sorted(staging.rglob("*")):
                if not staged.is_file():
                    continue
                relative = staged.relative_to(staging)
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = None
                if target.is_file():
                    backup = backup_root / ("amp-light-backup-" + uuid.uuid4().hex)
                    shutil.copyfile(target, backup)
                os.replace(staged, target)
                installed.append(target)
                backups.append((target, backup))
            verification = _verify_complete_donor(root, progress=progress)
            receipt = _write_bootstrap_receipt(root, package, manifest, installed, status="INSTALLED")
            _progress(progress, "complete", status="INSTALLED", files=len(installed))
            return {"status": "INSTALLED", "installed": [str(item) for item in installed], "receipt": receipt,
                    "restart_required": True, "network": False,
                    "verification": verification}
        except Exception as exc:
            failures = []
            for target, backup in reversed(backups):
                try:
                    if backup and backup.is_file():
                        os.replace(backup, target)
                    elif target.is_file():
                        target.unlink()
                except OSError as rollback_error:
                    failures.append("%s: %s" % (target, rollback_error))
            if failures:
                keep_backup = True
                raise OnlineComponentError("完整 donor 回滚失败，备份保留于 %s：%s" % (backup_root, "; ".join(failures))) from exc
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if not keep_backup:
                shutil.rmtree(backup_root, ignore_errors=True)
    finally:
        # ``temporary`` may be empty when the caller supplied a directory, but
        # it is still our owned scratch directory and must never accumulate.
        shutil.rmtree(temporary, ignore_errors=True)


def _artifact_path(cache_root, component_id, entry, app_version):
    from urllib.parse import urlsplit

    filename = Path(urlsplit(entry["urls"][0]).path).name or "artifact.bin"
    safe_id = component_id.replace(".", "_").replace("/", "_")
    safe_version = str(entry.get("version") or app_version).replace("/", "_")
    return cache_root / (safe_id + "-" + safe_version + "-" + filename)


def _run_checked(executable, arguments, *, env=None, timeout=60):
    if env is None:
        env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        result = subprocess.run([str(executable), *arguments], capture_output=True, text=True,
                                env=env, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OnlineComponentError("执行探针失败：%s" % exc) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-800:]
        raise OnlineComponentError("执行探针退出码 %s：%s" % (result.returncode, detail))
    return result


class _InstallTransaction:
    """Track replaced files so a later manifest/probe failure can roll back."""

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.backup_root = Path(tempfile.mkdtemp(prefix="amp-install-tx-", dir=str(self.root.parent)))
        self.records = []
        self._seen = set()

    def snapshot(self, target):
        target = Path(target).resolve()
        key = str(target).casefold()
        if key in self._seen:
            return
        self._seen.add(key)
        try:
            relative = target.relative_to(self.root)
        except ValueError as exc:
            raise OnlineComponentError("事务目标必须位于应用根目录内：" + str(target)) from exc
        backup = self.backup_root / relative
        if target.is_file():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, backup)
            self.records.append((target, backup, True))
        elif target.exists():
            raise OnlineComponentError("事务目标不是普通文件：" + str(target))
        else:
            self.records.append((target, backup, False))

    def rollback(self):
        failures = []
        for target, backup, existed in reversed(self.records):
            try:
                if existed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
                elif target.is_file():
                    target.unlink()
            except OSError as exc:
                failures.append("%s: %s" % (target, exc))
        if failures:
            # Keep the backup directory for manual recovery; silently deleting
            # the only copy would turn a failed install into data loss.
            raise OnlineComponentError("安装回滚失败，备份保留于 %s：%s" % (self.backup_root, "; ".join(failures)))
        shutil.rmtree(self.backup_root, ignore_errors=True)

    def commit(self):
        shutil.rmtree(self.backup_root, ignore_errors=True)


def _install_archive(root, artifact, entry, *, progress=None, transaction=None):
    """Install a pinned archive through a staging directory and probe its tools."""
    stage_root = Path(tempfile.mkdtemp(prefix="amp-archive-stage-", dir=str(root.parent)))
    try:
        extracted = stage_root / "extracted"
        _safe_zip_extract(artifact, extracted)
        ffmpeg = next(extracted.rglob("ffmpeg.exe"), None)
        ffprobe = next(extracted.rglob("ffprobe.exe"), None)
        if not ffmpeg or not ffprobe:
            raise OnlineComponentError("FFmpeg 归档缺少 ffmpeg.exe/ffprobe.exe")
        bin_dir = ffmpeg.parent
        if ffprobe.parent != bin_dir:
            raise OnlineComponentError("FFmpeg 归档中的 ffmpeg/ffprobe 不在同一 bin 目录")
        # Shared FFmpeg builds need every DLL next to the two executables.
        targets = {"runtime/bin/" + source.name: source for source in sorted(bin_dir.iterdir()) if source.is_file()}
        if "runtime/bin/ffmpeg.exe" not in targets or "runtime/bin/ffprobe.exe" not in targets:
            raise OnlineComponentError("FFmpeg bin 目录缺少可执行文件")
        installed = []
        local_transaction = transaction or _InstallTransaction(root)
        try:
            for relative, source in targets.items():
                relative = _safe_relative(relative)
                target = root / Path(*relative.split("/"))
                staged = stage_root / "staged" / Path(*relative.split("/"))
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, staged)
                if _sha256(staged) != _sha256(source):
                    raise OnlineComponentError("归档暂存校验失败：" + relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                local_transaction.snapshot(target)
                os.replace(staged, target)
                installed.append(target)
            _run_checked(root / "runtime/bin/ffmpeg.exe", ["-version"], timeout=30)
            _run_checked(root / "runtime/bin/ffprobe.exe", ["-version"], timeout=30)
        except Exception:
            if transaction is None:
                local_transaction.rollback()
            raise
        if transaction is None:
            local_transaction.commit()
        _progress(progress, "installed", component_id=entry.get("component_id"), files=len(installed))
        return {"files": [str(item) for item in installed], "probe": "passed"}
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _install_wheels(root, artifacts, entries, python_executable, *, progress=None, site_packages=None, transaction=None):
    """Install a verified wheel closure into the application runtime atomically."""
    python = Path(python_executable).resolve(strict=True)
    stage_root = Path(tempfile.mkdtemp(prefix="amp-wheel-stage-", dir=str(root.parent)))
    package_stage = stage_root / "site-packages"
    installed = []
    try:
        for item in artifacts:
            _progress(progress, "pip_start", component_id=item["component_id"])
            pip_filename = item.get("pip_filename") or Path(item["artifact"]).name
            pip_artifact = stage_root / pip_filename
            if Path(item["artifact"]).resolve() != pip_artifact.resolve():
                shutil.copyfile(item["artifact"], pip_artifact)
            _run_checked(python, ["-B", "-m", "pip", "install", "--disable-pip-version-check", "--no-index",
                                  "--no-deps", "--only-binary=:all:", "--upgrade", "--target", str(package_stage),
                                  str(pip_artifact)], timeout=600)
        site_packages = Path(site_packages) if site_packages else python.parent / "Lib" / "site-packages"
        site_packages = site_packages.resolve()
        site_packages.mkdir(parents=True, exist_ok=True)
        # Probe the complete staged closure before touching the application
        # site-packages.  This prevents a missing dependency from leaving a
        # partially replaced import tree behind.
        env = os.environ.copy()
        for entry in entries:
            module = entry.get("import_probe")
            if module:
                probe = "import sys; sys.path.insert(0, %r); import %s; print(%s.__name__)" % (str(package_stage), module, module)
                _run_checked(python, ["-B", "-S", "-c", probe], env=env, timeout=120)
            _progress(progress, "pip_complete", component_id=entry["component_id"], import_probe=module)
        try:
            site_packages.relative_to(root)
            site_inside_root = True
        except ValueError:
            site_inside_root = False
        local_transaction = transaction
        if local_transaction is None and site_inside_root:
            local_transaction = _InstallTransaction(root)
        # External test destinations are allowed; only application paths need
        # transaction snapshots.  The normal installer always stays in root.
        try:
            for source in sorted(package_stage.rglob("*")):
                if not source.is_file():
                    continue
                relative = source.relative_to(package_stage)
                target = site_packages / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if local_transaction is not None:
                    local_transaction.snapshot(target)
                os.replace(source, target)
                installed.append(target)
        except Exception:
            if transaction is None and local_transaction is not None:
                local_transaction.rollback()
            raise
        if transaction is None and local_transaction is not None:
            local_transaction.commit()
        return {"files": [str(item) for item in installed], "probe": "passed"}
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _write_runtime_manifests(root, python_executable, installed_files, source_records, *, site_packages=None):
    """Write the separate lightweight Polish manifest without downgrading strict bundles."""
    root = Path(root).resolve(strict=True)
    python = Path(python_executable).resolve(strict=True)
    try:
        executable = python.relative_to(root).as_posix()
    except ValueError as exc:
        raise OnlineComponentError("轻量 runtime/python.exe 必须位于应用根目录内") from exc
    site_root = Path(site_packages).resolve() if site_packages else python.parent / "Lib" / "site-packages"
    try:
        site_root.relative_to(root)
    except ValueError as exc:
        raise OnlineComponentError("轻量 Python site-packages 必须位于应用根目录内") from exc
    version_result = _run_checked(python, ["-B", "-c", "import platform; print(platform.python_version())"])
    python_version = version_result.stdout.strip().splitlines()[-1]
    module_names = ["tkinter", "numpy", "scipy", "soundfile", "cffi", "pycparser", "mido"]
    probe_code = (
        "import importlib, importlib.metadata, json, pathlib, sys; "
        "sys.path.insert(0, %r); result={}; "
        "names=%r; "
        "\nfor name in names:\n"
        "    try:\n"
        "        module=importlib.import_module(name); origin=getattr(module, '__file__', None); version=None\n"
        "        for dist in importlib.metadata.distributions():\n"
        "            if (dist.metadata.get('Name') or '').lower().replace('_','-') == name.lower().replace('_','-'):\n"
        "                version=dist.version; break\n"
        "        result[name]={'status':'FOUND','origin':origin,'version':version}\n"
        "    except Exception as exc:\n"
        "        result[name]={'status':'IMPORT_FAILED','error':type(exc).__name__+': '+str(exc)}\n"
        "print(json.dumps(result, ensure_ascii=False, sort_keys=True))"
    ) % (str(site_root), module_names)
    probe = _run_checked(python, ["-B", "-c", probe_code], timeout=180)
    try:
        modules = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise OnlineComponentError("轻量模块探针返回无效 JSON") from exc
    failed_modules = [name for name, item in modules.items() if item.get("status") != "FOUND"]
    if failed_modules:
        raise OnlineComponentError("轻量模块导入失败：" + ", ".join(sorted(failed_modules)))
    for name, item in modules.items():
        origin = item.get("origin")
        if origin:
            try:
                item["origin"] = Path(origin).resolve().relative_to(root).as_posix()
            except ValueError as exc:
                raise OnlineComponentError("轻量模块来自应用根目录外：" + name) from exc

    # Imports and pip may have left bytecode caches in the sealed program tree.
    # The modular verifier intentionally rejects those mutable artifacts.
    for cache_dir in root.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
    for bytecode in root.rglob("*.pyc"):
        try:
            bytecode.unlink()
        except OSError:
            pass

    inventory_roots = [root / "AI_Music_Processor.pyw", root / "process_music.py", root / "VERSION",
                       root / "core", root / "runtime", root / "tools" / "RVC" / "RVC-beta-v2-0618" / "runtime"]
    inventory = {}
    for base in inventory_roots:
        if not base.exists():
            continue
        paths = [base] if base.is_file() else list(base.rglob("*"))
        for path in paths:
            if not path.is_file() or "__pycache__" in path.parts or path.suffix.casefold() in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(root).as_posix()
            role = "application"
            if relative.casefold().startswith("tools/rvc/"):
                role = "python_runtime" if relative.casefold().endswith("/python.exe") else "site_package_or_runtime"
            elif relative.casefold().startswith("runtime/bin/"):
                role = "ffmpeg_runtime"
            inventory[relative.casefold()] = {"path": relative, "size": path.stat().st_size,
                                               "sha256": _sha256(path), "roles": [role]}
    for path in installed_files:
        path = Path(path)
        if path.is_file():
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise OnlineComponentError("安装文件不在应用根目录内：" + str(path)) from exc
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        target = root / "runtime" / "bin" / (name + ".exe")
        version_line = _run_checked(target, ["-version"], timeout=30).stdout.splitlines()[0]
        relative = target.relative_to(root).as_posix()
        tools[name] = {"path": relative, "version_line": version_line, "sha256": _sha256(target)}
    runtime_manifest = root / "runtime-manifest"
    runtime_manifest.mkdir(parents=True, exist_ok=True)
    bootstrap = {
        "schema_version": SCHEMA_VERSION,
        "profile": "modular-polish-v1",
        "status": "INSTALLED",
        "allowed_modes": ["polish"],
        "python": {"executable": executable, "version": python_version, "implementation": "CPython", "tkinter": True},
        "modules": modules,
        "tools": tools,
        "files": sorted(inventory.values(), key=lambda item: item["path"].casefold()),
        "sources": source_records,
        "user_data_preserved": True,
        "note": "Modular lightweight Polish bootstrap; strict full-bundle runtime.json remains untouched.",
    }
    target = runtime_manifest / "bootstrap-runtime.json"
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(_json_bytes(bootstrap))
    os.replace(temporary, target)
    return bootstrap


def install(root, source_manifest, requested=None, *, cache=None, offline_package=None,
            progress=None, cancel=None, cache_only=False, python_executable=None,
            receipt_root=None, site_packages=None):
    """Download and install a verified component closure, or a complete donor."""
    root = Path(root).resolve(strict=True)
    if offline_package:
        return _install_complete_donor(offline_package, root, progress=progress)
    if (root / "runtime-manifest" / "runtime.json").is_file() or (root / "release-manifest.json").is_file():
        raise OnlineComponentError("当前目录是完整 strict release；在线 Polish bootstrap 会覆盖其受管版本，必须改用同版本完整 donor/.ampkg")
    plan_result = plan(root, source_manifest, requested)
    document, sources = load_sources(source_manifest, catalog=load_catalog(root / "runtime-manifest" / "components.json"),
                                     version=plan_result["app_version"])
    bootstrap = load_bootstrap_packages(document)
    cache_root = Path(cache or (root / "component-cache")).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    acquired, blocked, installable = [], [], []
    source_records = {}
    entries = []
    for row in plan_result["rows"]:
        if cancel and cancel():
            raise OnlineComponentError("用户取消组件安装")
        entry = bootstrap.get(row["component_id"]) if row.get("source_kind") == "bootstrap" else sources[row["component_id"]]
        if entry["status"] != "PINNED":
            if entry["status"] in {"UNAVAILABLE", "USER_OWNED_ONLY"}:
                blocked.append({"component_id": row["component_id"], "status": entry["status"], "reason": entry["reason"]})
            continue
        source_records[row["component_id"]] = {
            "version": entry.get("version"), "sha256": entry.get("sha256"),
            "size_bytes": entry.get("size_bytes"), "artifact": Path(urllib.parse.urlsplit(entry["urls"][0]).path).name
        }
        if entry.get("install_strategy") == "native_installer":
            if not python_executable or not Path(python_executable).is_file():
                blocked.append({"component_id": row["component_id"], "status": "NOT_READY", "reason": "PowerShell must install and pass Tk Python first"})
            else:
                acquired.append({"component_id": row["component_id"], "status": "ALREADY_PRESENT", "artifact": None})
            continue
        artifact = _artifact_path(cache_root, row["component_id"], entry, plan_result["app_version"])
        result = download_verified(entry["urls"], str(artifact), entry["sha256"], entry["allowed_hosts"],
                                   max_bytes=max(entry["size_bytes"], 1), progress=lambda value: _progress(
                                       progress, "download", component_id=row["component_id"], **value),
                                   check_cancel=(lambda: (_ for _ in ()).throw(OnlineComponentError("用户取消组件安装")) if cancel and cancel() else None))
        record = {"component_id": row["component_id"], "artifact": str(artifact), "result": result,
                  "install_strategy": entry.get("install_strategy"),
                  "pip_filename": Path(urllib.parse.urlsplit(entry["urls"][0]).path).name}
        acquired.append(record)
        entries.append((row, entry, record))
        if entry.get("install_strategy") in {"archive_install", "pip_install"}:
            installable.append((row, entry, record))
    # A cache-only request such as ``runtime.faiss`` may inspect the artifact
    # without having a Tk runtime yet.  Keep the receipt truthful while not
    # turning that prerequisite into a spurious external-source block.
    if any(entry.get("install_strategy") == "cache_only" for _row, entry, _record in entries):
        blocked = [item for item in blocked if item.get("status") != "NOT_READY"]
    # Receipts are mutable user state.  Keep the default outside the immutable
    # program root as well as the PowerShell entry point, so direct callers
    # cannot accidentally write online progress/results beside the manifest.
    if receipt_root:
        receipt_root = Path(receipt_root).resolve()
    else:
        data_root = os.environ.get("AI_MUSIC_PROCESSOR_DATA_ROOT")
        if data_root:
            receipt_root = Path(data_root).expanduser().resolve() / "logs"
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            base = Path(local_app_data).expanduser() if local_app_data else root.parent
            receipt_root = (base / "AI-Music-Processor" / "logs").resolve()
    receipt_root.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_root / "online-components-receipt.json"
    if blocked and not cache_only:
        receipt = {"schema_version": SCHEMA_VERSION, "status": "BLOCKED_EXTERNAL_SOURCE", "app_version": plan_result["app_version"],
                   "component_ids": plan_result["component_ids"], "bootstrap_packages": plan_result["bootstrap_packages"],
                   "acquired": acquired, "blocked": blocked, "user_data_preserved": True}
        receipt_path.write_bytes(_json_bytes(receipt))
        _progress(progress, "complete", status=receipt["status"], acquired=len(acquired), blocked=len(blocked))
        raise OnlineComponentError("所选组件仍有不可自动安装项：" + "; ".join(item["component_id"] for item in blocked))
    if cache_only or any(entry.get("install_strategy") == "cache_only" for _row, entry, _record in entries):
        receipt = {"schema_version": SCHEMA_VERSION, "status": "CACHED_NOT_INSTALLED", "app_version": plan_result["app_version"],
                   "component_ids": plan_result["component_ids"], "bootstrap_packages": plan_result["bootstrap_packages"],
                   "acquired": acquired, "blocked": blocked, "user_data_preserved": True}
        receipt_path.write_bytes(_json_bytes(receipt))
        _progress(progress, "complete", status=receipt["status"], acquired=len(acquired), blocked=len(blocked))
        if not cache_only:
            raise OnlineComponentError("来源已缓存但 install_strategy=cache_only；请使用同版本 donor/ampkg")
        return receipt
    if not python_executable:
        raise OnlineComponentError("在线安装需要 PowerShell 传入带 Tk 的 runtime/python.exe")
    archive_records = [record for row, entry, record in installable if entry.get("install_strategy") == "archive_install"]
    wheel_records = [record for row, entry, record in installable if entry.get("install_strategy") == "pip_install"]
    installed_files = []
    transaction = _InstallTransaction(root)
    try:
        # bootstrap-runtime.json is mutable installation state too; restore an
        # earlier valid manifest if a later import/hash check fails.
        transaction.snapshot(root / "runtime-manifest" / "bootstrap-runtime.json")
        if archive_records:
            for record in archive_records:
                installed_files.extend(_install_archive(root, record["artifact"], sources[record["component_id"]],
                                                        progress=progress, transaction=transaction)["files"])
        if wheel_records:
            wheel_entries = [entry for _row, entry, record in installable if record in wheel_records]
            installed_files.extend(_install_wheels(root, wheel_records, wheel_entries, python_executable,
                                                   progress=progress, site_packages=site_packages,
                                                   transaction=transaction)["files"])
        bootstrap_manifest = _write_runtime_manifests(root, python_executable, installed_files, source_records,
                                                      site_packages=site_packages)
        receipt = {"schema_version": SCHEMA_VERSION, "status": "INSTALLED", "app_version": plan_result["app_version"],
                   "component_ids": plan_result["component_ids"], "bootstrap_packages": plan_result["bootstrap_packages"],
                   "acquired": acquired, "blocked": blocked, "installed_files": [str(item) for item in installed_files],
                   "user_data_preserved": True, "runtime_manifest": "runtime-manifest/bootstrap-runtime.json",
                   "allowed_modes": bootstrap_manifest["allowed_modes"]}
        receipt_path.write_bytes(_json_bytes(receipt))
        transaction.commit()
        _progress(progress, "complete", status="INSTALLED", acquired=len(acquired), blocked=len(blocked))
        return receipt
    except Exception:
        transaction.rollback()
        raise


def _cli_progress(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI Music Processor lightweight component bootstrap")
    parser.add_argument("command", choices=("plan", "install"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--components", default="all")
    parser.add_argument("--cache")
    parser.add_argument("--offline-package")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--python", dest="python_executable")
    parser.add_argument("--receipt-root")
    parser.add_argument("--site-packages")
    args = parser.parse_args(argv)
    try:
        requested = args.components
        result = plan(args.root, args.manifest, requested) if args.command == "plan" else install(
            args.root, args.manifest, requested, cache=args.cache, offline_package=args.offline_package,
            progress=_cli_progress, cache_only=args.cache_only, python_executable=args.python_executable,
            receipt_root=args.receipt_root, site_packages=args.site_packages)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"event": "failed", "status": "FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
