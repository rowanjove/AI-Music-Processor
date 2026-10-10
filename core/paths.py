"""Small, side-effect-free helpers for identifying a packaged release."""
import os
from pathlib import Path


PORTABLE_RELEASE_MARKER = "PORTABLE_RELEASE"
PORTABLE_RELEASE_CONTENT = "AI Music Processor Portable Release v1"
RELEASE_MANIFEST = "release-manifest.json"
DATA_ROOT_ENV = "AI_MUSIC_PROCESSOR_DATA_ROOT"


def is_release_mode(base_dir=None):
    """Return whether this application root is marked as a portable release.

    A release marker or root release manifest keeps a package in release mode,
    including when the marker is damaged. The verifier validates its contents.
    Development workspaces therefore keep their existing path resolution.
    """
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent.parent
    return (os.path.lexists(str(root / PORTABLE_RELEASE_MARKER)) or
            os.path.lexists(str(root / RELEASE_MANIFEST)))


def release_data_root(environ=None):
    """Return the release data root without creating or migrating anything.

    ``AI_MUSIC_PROCESSOR_DATA_ROOT`` is an explicit absolute override for
    isolated tests and operator-managed deployments. Normal Windows releases
    use ``%LOCALAPPDATA%\\AI-Music-Processor``.
    """
    values = os.environ if environ is None else environ
    override = values.get(DATA_ROOT_ENV)
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError(DATA_ROOT_ENV + " must be an absolute path")
        return path.resolve()
    local_app_data = values.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / "AppData" / "Local"
    return (base / "AI-Music-Processor").resolve()


def release_numba_cache_dir(data_root=None, environ=None):
    """Return the isolated Numba JIT cache path for a release process."""
    root = Path(data_root).resolve() if data_root is not None else release_data_root(environ)
    return root / "cache" / "numba"


def application_data_paths(base_dir=None, environ=None, release_mode=None):
    """Map mutable application state to legacy dev paths or release user data.

    This helper is side-effect free. Callers create only the directories they
    need after a release runtime gate has succeeded.
    """
    app_root = (Path(os.path.abspath(os.fspath(base_dir))) if base_dir is not None
                else Path(__file__).resolve().parent.parent)
    strict = is_release_mode(app_root) if release_mode is None else bool(release_mode)
    data_root = release_data_root(environ) if strict else app_root
    if strict:
        resolved_app_root = app_root.resolve()
        try:
            data_root.relative_to(resolved_app_root)
        except ValueError:
            pass
        else:
            raise ValueError("release data root must be outside the immutable program tree")
    return {
        "data_root": data_root,
        "config": data_root / "config",
        "output": data_root / "output",
        "temp": data_root / "temp",
        "logs": data_root / "logs",
        "database": data_root / "output" / "database" / "app.db",
    }
