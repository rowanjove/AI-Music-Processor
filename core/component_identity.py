"""Record the mode pack and its direct dependencies on a new job.

The record is the catalog id, version, and a hash of that catalog entry.
It does not hash weights or runtime files, and resume does not compare it
with a later catalog. An old snapshot without this field still restores.
"""
import hashlib

from core.component_platform import catalog_path, load_catalog
from core.storage import canonical_json

MODES = ("polish", "voice_conversion", "transcribe")
_HEX = "0123456789abcdef"


def declaration_sha256(component):
    return hashlib.sha256(canonical_json(component).encode("utf-8")).hexdigest()


def capture_component_identity(mode, root, separator_profile=None, f0_method=None):
    """Copy today's catalog identity. Caller stores it; this does not write."""
    if mode not in MODES:
        raise ValueError("没有对应的模式组件")
    catalog = load_catalog(catalog_path(root))
    by_id = {item["id"]: item for item in catalog["components"]}
    mode_id = "mode." + mode
    mode_component = by_id.get(mode_id)
    if mode_component is None:
        raise ValueError("目录里没有模式组件")
    rows = []
    dependencies = list(mode_component.get("dependencies") or [])
    if mode == "voice_conversion" and separator_profile is not None:
        dependencies = [item for item in dependencies if not item.startswith("engine.separator.")]
        for side in ("vocal", "instrumental"):
            component_id = (separator_profile.get(side) or {}).get("component_id")
            if component_id and component_id not in dependencies:
                dependencies.append(component_id)
        if separator_profile.get("mode") == "dual" and "engine.separator.mdx" not in dependencies:
            dependencies.append("engine.separator.mdx")
    if mode == "voice_conversion" and f0_method == "rmvpe":
        dependencies.append("engine.f0.rmvpe")
    for component_id in [mode_id] + dependencies:
        component = by_id.get(component_id)
        if component is None:
            raise ValueError("模式依赖的组件不在目录中")
        rows.append({
            "component_id": component["id"],
            "version": component.get("version"),
            "declaration_sha256": declaration_sha256(component),
        })
    rows.sort(key=lambda item: item["component_id"])
    return rows


def validate_component_identity(value):
    """Check shape only. Do not reread the catalog or model files."""
    if not isinstance(value, list) or not value:
        raise ValueError("component_identity 必须是非空列表")
    seen = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"component_id", "version", "declaration_sha256"}:
            raise ValueError("component_identity 条目字段不正确")
        if not isinstance(item["component_id"], str) or not item["component_id"]:
            raise ValueError("component_identity 缺少 component_id")
        if item["version"] is not None and not isinstance(item["version"], str):
            raise ValueError("component_identity 的 version 必须是字符串或空")
        digest = item["declaration_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in _HEX for char in digest):
            raise ValueError("component_identity 哈希无效")
        seen.append(item["component_id"])
    if seen != sorted(seen) or len(set(seen)) != len(seen):
        raise ValueError("component_identity 必须按 component_id 排序且不重复")
    return value
