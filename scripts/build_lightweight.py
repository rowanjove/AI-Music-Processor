"""Build a deterministic, source-only GitHub distribution.

The result intentionally excludes all installed runtimes, vendor trees,
models, audio, databases, logs, and prior build output.  It contains the
native PowerShell bootstrap and the checked-in source/download policy only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT_FILES = {
    "AI_Music_Processor.pyw", "CHANGELOG.md", "CONTRIBUTING.md",
    "LICENSE", "README.md", "THIRD_PARTY_LICENSES.md", "VERSION", "process_music.py",
    "启动AI音乐一体化处理器.bat",
}
OPTIONAL_ROOT_FILES = {"PORTABLE_RELEASE"}
ROOT_DIRS = {"app", "core", "tests"}
DIRECTORY_ALLOWLIST = {
    "docs": {"LIGHTWEIGHT_RELEASE.md"},
    "packaging": {"download-sources.json", "editor-sources.json", "lightweight_profile.json", "portable_profile.json"},
    "scripts": {"build_lightweight.py", "install_components.ps1"},
}
EXCLUDED_ROOTS = {
    "runtime", "tools", "models", "output", "song", "config", "logs", "temp", "dist", "文档归档",
    ".git", ".venv", "__pycache__",
}
EXCLUDED_SUFFIXES = {
    ".exe", ".dll", ".pyd", ".pth", ".pt", ".onnx", ".index", ".wav", ".mp3", ".flac", ".mid", ".zip",
}
GENERATED_MARKERS = {
    "LIGHTWEIGHT_RELEASE": b"AI Music Processor Lightweight Release v1\n",
    "PORTABLE_RELEASE": b"AI Music Processor Portable Release v1\n",
}
# The native bootstrap is rooted beside the source files.  Keeping exactly one
# root-level name also matches Launcher.cs' BaseDirectory lookup on a fresh
# GitHub checkout; an app/ copy would resolve paths one directory too deep.
OPTIONAL_LAUNCHER_FILES = {"AI-Music-Processor.exe"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unsafe(path):
    try:
        info = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def source_files(root):
    root = Path(root).resolve(strict=True)
    files = []
    required_files = ROOT_FILES | {"runtime-manifest/components.json"}
    optional_files = {item for item in OPTIONAL_ROOT_FILES if (root / item).is_file()}
    for relative in sorted(required_files | optional_files):
        path = root / relative
        if not path.is_file() or _unsafe(path):
            raise FileNotFoundError("required lightweight source is missing or unsafe: " + relative)
        files.append((relative.replace("\\", "/"), path))
    for directory in sorted(ROOT_DIRS):
        base = root / directory
        if not base.is_dir() or _unsafe(base):
            raise FileNotFoundError("required lightweight source directory is missing or unsafe: " + directory)
        for path in sorted(base.rglob("*")):
            if not path.is_file() or _unsafe(path):
                continue
            relative = path.relative_to(root).as_posix()
            if any(part.casefold() in {item.casefold() for item in EXCLUDED_ROOTS} for part in Path(relative).parts):
                continue
            if path.suffix.casefold() in EXCLUDED_SUFFIXES:
                continue
            files.append((relative, path))
    for directory, allowed in sorted(DIRECTORY_ALLOWLIST.items()):
        base = root / directory
        if not base.is_dir() or _unsafe(base):
            raise FileNotFoundError("required lightweight source directory is missing or unsafe: " + directory)
        for name in sorted(allowed):
            path = base / name
            if not path.is_file() or _unsafe(path):
                raise FileNotFoundError("required public lightweight source is missing or unsafe: " + str(path.relative_to(root)))
            files.append((path.relative_to(root).as_posix(), path))
    # A freshly compiled first-party bootstrap is the sole binary exception.
    # Vendor/runtime/model executables remain excluded by suffix and root rules.
    for relative in sorted(OPTIONAL_LAUNCHER_FILES):
        path = root / relative
        if path.is_file() and not _unsafe(path):
            files.append((relative, path))
    unique = {}
    for relative, path in files:
        key = relative.casefold()
        if key in unique:
            raise ValueError("duplicate lightweight path: " + relative)
        unique[key] = (relative, path)
    return sorted(unique.values(), key=lambda item: item[0].casefold())


def _zip_info(name):
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def compile_bootstrap(root):
    """Build the portable bootstrap instead of reusing a developer EXE."""
    if os.name != 'nt':
        raise RuntimeError('Windows lightweight bootstrap must be built on Windows')
    windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    candidates = [windows/'Microsoft.NET/Framework64/v4.0.30319/csc.exe',
                  windows/'Microsoft.NET/Framework/v4.0.30319/csc.exe']
    compiler = next((p for p in candidates if p.is_file()), None)
    if compiler is None:
        raise RuntimeError('Missing Windows .NET Framework C# compiler')
    with tempfile.TemporaryDirectory(prefix='amp-light-bootstrap-') as folder:
        output = Path(folder)/'AI-Music-Processor.exe'
        result = subprocess.run([str(compiler), '/nologo', '/target:winexe',
            '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
            '/define:PORTABLE_BUILD', '/out:'+str(output), str(root/'app/Launcher.cs')],
            capture_output=True, timeout=90)
        if result.returncode or not output.is_file():
            raise RuntimeError('Portable bootstrap compilation failed: '+result.stderr.decode('utf-8',errors='replace'))
        return output.read_bytes()


def build(output, source_root=None):
    root = Path(source_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists() or os.path.lexists(str(output)):
        raise FileExistsError("refuse to overwrite existing lightweight artifact: " + str(output))
    files = [(name,path) for name,path in source_files(root) if name not in OPTIONAL_LAUNCHER_FILES]
    generated = dict(GENERATED_MARKERS)
    generated['AI-Music-Processor.exe'] = compile_bootstrap(root)
    version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    records = [{"path": relative, "size": path.stat().st_size, "sha256": sha256(path)} for relative, path in files]
    records.extend({"path": relative, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                   for relative, payload in sorted(generated.items()))
    manifest = {
        "schema_version": 1,
        "package_kind": "github-source-bootstrap",
        "profile": "github-source-win-x64",
        "version": version,
        "source_status": "SOURCE_ONLY",
        "heavy_assets": "excluded; acquire via scripts/install_components.ps1",
        "user_data": "excluded",
        "files": records,
        "excluded_roots": sorted(EXCLUDED_ROOTS),
        "excluded_extensions": sorted(EXCLUDED_SUFFIXES),
        "included_binary_exceptions": ['AI-Music-Processor.exe'],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("." + output.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive:
            for relative, path in files:
                archive.writestr(_zip_info(relative), path.read_bytes())
            for relative, payload in sorted(generated.items()):
                archive.writestr(_zip_info(relative), payload)
            archive.writestr(_zip_info("lightweight-manifest.json"), json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        os.replace(temporary, output)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    digest = sha256(output)
    sums = output.with_name(output.stem + "-SHA256SUMS.txt")
    with sums.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("%s  %s\n" % (digest, output.name))
    return {"status": "CREATED", "zip": str(output), "sha256": digest,
            "sha256sums": str(sums), "file_count": len(records),
            "source_bytes": sum(item["size"] for item in records), "zip_bytes": output.stat().st_size,
            "excluded_roots": sorted(EXCLUDED_ROOTS)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build source-only AI Music Processor distribution")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.output, args.source_root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
