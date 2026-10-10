import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.component_platform import (
    ComponentCatalogError,
    build_registry,
    capability_report,
    catalog_path,
    classify_voice_model,
    dependents,
    has_capability,
    load_catalog,
    model_profile_contract,
    scan_hardware,
)


def _component(component_id, kind="runtime", probes=None, **extra):
    component = {
        "id": component_id,
        "name": component_id,
        "kind": kind,
        "ownership": "managed",
        "required": True,
        "implemented": True,
        "capabilities": ["runtime.python"] if kind != "mode_pack" else ["mode.polish"],
        "modes": ["polish"],
        "dependencies": [],
        "probes": probes if probes is not None else [
            {"type": "relative_file", "path": "runtime/python.exe", "role": "required"}
        ],
        "license_status": "UNKNOWN",
        "distribution_status": "UNKNOWN",
    }
    component.update(extra)
    return component


def _catalog(*components):
    return {
        "schema_version": 1,
        "catalog_kind": "installed-workspace-description",
        "size_policy": "probe bytes only",
        "components": list(components),
    }


def _tool_probe():
    return {
        "type": "tool",
        "name": "ffmpeg",
        "release_path": "runtime/bin/ffmpeg.exe",
        "dev_paths": ["tools/ffmpeg/ffmpeg.exe", "tools/RVC/ffmpeg.exe"],
        "allow_host_in_dev": True,
        "host_before_dev_path_index": 1,
    }


class ComponentPlatformTests(unittest.TestCase):
    def test_workspace_catalog_matches_schema_enums(self):
        schema = json.loads((ROOT / "runtime-manifest" / "schema" / "components.schema.json").read_text(encoding="utf-8"))
        catalog = load_catalog(catalog_path(ROOT))
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(
            schema["$defs"]["component"]["properties"]["kind"]["enum"],
            ["application", "runtime", "tool", "engine", "model", "model_scan", "mode_pack"],
        )
        self.assertTrue(all(item["license_status"] == "UNKNOWN" for item in catalog["components"]))
        self.assertNotIn("download_size", json.dumps(catalog))

    def test_catalog_rejects_drift(self):
        with self.assertRaises(ComponentCatalogError):
            load_catalog_from_object = _catalog(_component("runtime.python", download_size=1))
            from core.component_platform import validate_catalog
            validate_catalog(load_catalog_from_object)
        from core.component_platform import validate_catalog
        with self.assertRaises(ComponentCatalogError):
            validate_catalog(_catalog(_component("runtime.python"), _component("runtime.python")))
        cycle = _catalog(
            _component("runtime.python", dependencies=["runtime.torch"], probes=[
                {"type": "relative_file", "path": "a.exe", "role": "required"}
            ]),
            _component("runtime.torch", capabilities=["runtime.torch"], dependencies=["runtime.python"], probes=[
                {"type": "relative_file", "path": "b.exe", "role": "required"}
            ]),
        )
        with self.assertRaises(ComponentCatalogError):
            validate_catalog(cycle)
        hidden = _catalog(_component(
            "engine.f0.rmvpe", kind="engine", implemented=False, capabilities=["pitch.rmvpe"], probes=[]
        ))
        with self.assertRaises(ComponentCatalogError):
            validate_catalog(hidden)
        with self.assertRaises(ComponentCatalogError):
            validate_catalog(_catalog(_component(
                "engine.separator.roformer", kind="engine", details={"minimum_python_version": [3, True]}
            )))

    def test_registry_reports_presence_without_calling_runtime_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime").mkdir()
            (root / "runtime" / "python.exe").write_bytes(b"python")
            catalog = _catalog(_component("runtime.python"))
            with mock.patch("core.runtime_verify.verify_runtime") as verify:
                registry = build_registry(root, catalog=catalog, release_mode=False)
                verify.assert_not_called()
            self.assertFalse(registry["verifier"]["invoked_by_registry"])
            self.assertEqual(registry["components"]["runtime.python"]["status"], "PRESENT")
            self.assertEqual(registry["components"]["runtime.python"]["probed_bytes"], 6)
            self.assertEqual(registry["components"]["runtime.python"]["footprint"], "PARTIAL_PROBES_ONLY")

    def test_minimum_python_uses_frozen_runtime_version_and_keeps_file_evidence(self):
        component = _component(
            "engine.separator.roformer", kind="engine", ownership="external", required=False,
            capabilities=["separator.two_stem.roformer"],
            details={"minimum_python_version": [3, 10]},
            probes=[{"type": "relative_file", "path": "candidate.py", "role": "required"}],
        )
        catalog = _catalog(component)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "candidate.py").write_text("adapter", encoding="utf-8")
            runtime_dir = root / "runtime-manifest"
            runtime_dir.mkdir()
            (runtime_dir / "runtime.json").write_text(
                json.dumps({"python": {"version": "3.9.13"}}), encoding="utf-8")
            old_runtime = build_registry(root, catalog=catalog, release_mode=True)
            record = old_runtime["components"]["engine.separator.roformer"]
            self.assertTrue(record["implemented"])
            self.assertEqual(record["status"], "INCOMPATIBLE_RUNTIME")
            self.assertIn("3.9", record["unavailable_reason"])
            self.assertEqual(record["files"][0]["relative"], "candidate.py")
            self.assertFalse(has_capability(capability_report(old_runtime), "separator.two_stem.roformer"))

            (runtime_dir / "runtime.json").write_text(
                json.dumps({"python": {"version": "3.10.2"}}), encoding="utf-8")
            compatible_runtime = build_registry(root, catalog=catalog, release_mode=True)
            self.assertEqual(compatible_runtime["components"]["engine.separator.roformer"]["status"], "PRESENT")
            self.assertTrue(has_capability(capability_report(compatible_runtime), "separator.two_stem.roformer"))

            (runtime_dir / "runtime.json").write_text("{}", encoding="utf-8")
            unknown_runtime = build_registry(root, catalog=catalog, release_mode=True)
            self.assertEqual(unknown_runtime["components"]["engine.separator.roformer"]["status"], "UNKNOWN_RUNTIME")
            self.assertFalse(has_capability(capability_report(unknown_runtime), "separator.two_stem.roformer"))

    def test_release_mode_ignores_host_and_dev_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vendor = root / "tools" / "RVC"
            vendor.mkdir(parents=True)
            (vendor / "ffmpeg.exe").write_bytes(b"vendor")
            catalog = _catalog(_component(
                "tool.ffmpeg", kind="tool", capabilities=["audio.decode"], probes=[_tool_probe()]
            ))
            with mock.patch("core.component_platform._host_tool", return_value=None):
                dev = build_registry(root, catalog=catalog, release_mode=False)
                release = build_registry(root, catalog=catalog, release_mode=True)
            self.assertEqual(dev["components"]["tool.ffmpeg"]["status"], "PRESENT")
            self.assertTrue(dev["components"]["tool.ffmpeg"]["files"][0]["relative"].endswith("tools/RVC/ffmpeg.exe"))
            self.assertEqual(release["components"]["tool.ffmpeg"]["status"], "MISSING")

    def test_unimplemented_capability_stays_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "rmvpe.pt").write_bytes(b"not-wired")
            catalog = _catalog(_component(
                "engine.f0.rmvpe",
                kind="engine",
                implemented=False,
                capabilities=["pitch.rmvpe"],
                probes=[],
                unavailable_reason="not wired",
            ))
            report = capability_report(build_registry(root, catalog=catalog, release_mode=True))
            self.assertFalse(has_capability(report, "pitch.rmvpe"))

    def test_user_and_managed_models_stay_separated(self):
        self.assertEqual(classify_voice_model(
            "models/rvc/custom/voice.pth", ["zhoujielun.pth"], "tools/RVC/weights", "models/rvc"
        ), "user")
        self.assertEqual(classify_voice_model(
            "tools/RVC/weights/zhoujielun.pth", ["zhoujielun.pth"], "tools/RVC/weights", "models/rvc"
        ), "managed")
        self.assertEqual(classify_voice_model(
            "tools/RVC/weights/extra.pth", ["zhoujielun.pth"], "tools/RVC/weights", "models/rvc"
        ), "user")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            weights = root / "tools" / "RVC" / "weights"
            logs = root / "tools" / "RVC" / "logs" / "extra"
            user = root / "models" / "rvc" / "album"
            weights.mkdir(parents=True)
            logs.mkdir(parents=True)
            user.mkdir(parents=True)
            (weights / "zhoujielun.pth").write_bytes(b"managed")
            (weights / "extra.pth").write_bytes(b"extra")
            (logs / "voice.index").write_bytes(b"index")
            (user / "album.pth").write_bytes(b"user")
            catalog = _catalog(_component(
                "model.rvc.user_scan",
                kind="model_scan",
                ownership="user",
                required=False,
                capabilities=["voice.model"],
                probes=[{
                    "type": "user_rvc_models",
                    "weights_dir": "tools/RVC/weights",
                    "logs_dir": "tools/RVC/logs",
                    "user_root": "models/rvc",
                    "managed_files": ["zhoujielun.pth"],
                }],
            ))
            record = build_registry(root, catalog=catalog, release_mode=True)["components"]["model.rvc.user_scan"]
            found = {item["key"]: item for item in record["user_models"]}
            self.assertEqual(set(found), {"album", "extra"})
            self.assertEqual(found["album"]["ownership"], "user")
            self.assertEqual(found["extra"]["index"], "tools/RVC/logs/extra/voice.index")
            self.assertNotIn("zhoujielun.pth", json.dumps(record["user_models"]))

    def test_recorded_hash_compares_size_and_optional_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime-manifest").mkdir()
            (root / "model.bin").write_bytes(b"abcd")
            (root / "runtime-manifest" / "models.json").write_text(json.dumps({
                "assets": [{"files": [{"path": "model.bin", "sha256": "88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589", "size": 4}]}]
            }), encoding="utf-8")
            catalog = _catalog(_component("model.hubert", kind="model", capabilities=["voice.feature_extractor"], probes=[
                {"type": "relative_file", "path": "model.bin", "role": "required"}
            ]))
            unchecked = build_registry(root, catalog=catalog, release_mode=True, verify_hashes=False)
            file_record = unchecked["components"]["model.hubert"]["files"][0]
            self.assertEqual(file_record["hash_status"], "RECORDED_SIZE_MATCH")
            self.assertIsNone(file_record["sha256"])
            checked = build_registry(root, catalog=catalog, release_mode=True, verify_hashes=True)
            self.assertEqual(checked["components"]["model.hubert"]["files"][0]["hash_status"], "VERIFIED")
            (root / "runtime-manifest" / "models.json").write_text(json.dumps({
                "assets": [{"files": [{"path": "model.bin", "sha256": "aa", "size": 9}]}]
            }), encoding="utf-8")
            stale = build_registry(root, catalog=catalog, release_mode=True)
            self.assertEqual(stale["components"]["model.hubert"]["files"][0]["hash_status"], "STALE_RECORD")

    def test_mode_closure_blocks_missing_shared_runtime(self):
        python = _component("runtime.python")
        torch = _component(
            "runtime.torch", capabilities=["runtime.torch"], dependencies=["runtime.python"],
            probes=[{"type": "relative_file", "path": "torch.txt", "role": "required"}],
        )
        voice = _component(
            "mode.voice_conversion", kind="mode_pack", capabilities=["mode.voice_conversion"],
            modes=["voice_conversion"], dependencies=["runtime.torch"], probes=[],
        )
        polish = _component(
            "mode.polish", kind="mode_pack", capabilities=["mode.polish"],
            dependencies=["runtime.python"], probes=[],
        )
        catalog = _catalog(python, torch, polish, voice)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime").mkdir()
            (root / "runtime" / "python.exe").write_bytes(b"py")
            registry = build_registry(root, catalog=catalog, release_mode=True)
            report = capability_report(registry)
            self.assertEqual(report["modes"]["polish"]["status"], "AVAILABLE")
            self.assertEqual(report["modes"]["voice_conversion"]["status"], "BLOCKED")
            self.assertIn("runtime.torch", report["modes"]["voice_conversion"]["missing"])
            self.assertEqual(dependents(report, "runtime.python"), ["mode.polish", "mode.voice_conversion"])
            self.assertEqual(dependents(report, "runtime.torch"), ["mode.voice_conversion"])
            self.assertIn("runtime.python", report["shared_dependencies"])

    def test_profile_contract_uses_the_existing_validator(self):
        contract = model_profile_contract()
        self.assertFalse(contract["second_profile_system"])
        self.assertEqual(contract["job_spec_schema_version"], 1)
        self.assertEqual(contract["f0_methods_accepted_by_config"], ["harvest", "pm", "crepe", "rmvpe"])
        self.assertEqual(contract["f0_methods_not_implemented"], [])
        self.assertEqual(contract["defaults"]["f0_method"], "harvest")

    def test_hardware_scan_does_not_probe_gpu(self):
        hardware = scan_hardware(ROOT)
        self.assertGreater(hardware["cpu_count"], 0)
        self.assertGreater(hardware["ram_total_bytes"], 0)
        self.assertGreater(hardware["disk_free_bytes"], 0)
        self.assertEqual(hardware["gpu"]["status"], "NOT_PROBED")

    def test_installed_workspace_registry(self):
        registry = build_registry(ROOT)
        report = capability_report(registry)
        components = registry["components"]
        self.assertEqual(components["engine.f0.rmvpe"]["status"], "PRESENT")
        self.assertEqual(components["engine.separator.mdx"]["status"], "PRESENT")
        self.assertTrue(components["engine.separator.mdx"]["implemented"])
        self.assertEqual(components["engine.separator.kim_vocal_2"]["status"], "PRESENT")
        self.assertEqual(components["engine.separator.inst_hq_3"]["status"], "PRESENT")
        self.assertEqual(components["engine.separator.roformer"]["status"], "INCOMPATIBLE_RUNTIME")
        self.assertTrue(components["engine.separator.roformer"]["implemented"])
        self.assertTrue(has_capability(report, "pitch.rmvpe"))
        self.assertTrue(has_capability(report, "separator.two_stem.mdx"))
        self.assertFalse(has_capability(report, "separator.two_stem.roformer"))
        self.assertTrue(has_capability(report, "separator.two_stem"))
        self.assertTrue(has_capability(report, "voice.rvc"))
        self.assertEqual(components["runtime.python"]["status"], "PRESENT")
        self.assertEqual(components["runtime.python"]["version_status"], "MATCH")
        self.assertEqual(components["runtime.torch"]["version_status"], "MATCH")
        self.assertEqual(components["app.core"]["version_status"], "MATCH")
        self.assertEqual(components["engine.separator.hp2"]["status"], "PRESENT")
        self.assertEqual(components["engine.separator.htdemucs"]["status"], "PRESENT")
        self.assertEqual(components["model.hubert"]["files"][0]["hash_status"], "RECORDED_SIZE_MATCH")
        self.assertIsNone(components["model.hubert"]["files"][0]["sha256"])
        self.assertLess(components["model.hubert"]["probed_bytes"], 1024 ** 3)
        user_names = {item["key"] for item in components["model.rvc.user_scan"]["user_models"]}
        self.assertTrue(user_names.isdisjoint({"zhoujielun", "sunyanzi", "zhaolusi"}))
        for mode in ("polish", "voice_conversion", "transcribe"):
            self.assertEqual(report["modes"][mode]["status"], "AVAILABLE", report["modes"][mode])
        self.assertEqual(
            registry["paths"]["tools/RVC/RVC-beta-v2-0618/runtime/python.exe"],
            ["runtime.python"],
        )
        self.assertGreater(len(dependents(report, "runtime.python")), 1)
        import inspect
        from core import component_platform as platform
        registry_source = inspect.getsource(platform.build_registry)
        self.assertNotIn("verify_runtime", registry_source)
        self.assertNotIn("pipeline", registry_source)

    def test_capability_report_with_runtime_verification_and_reasons(self):
        registry = build_registry(ROOT)
        # 1. Without runtime_verification
        report_default = capability_report(registry)
        for mode in ("polish", "voice_conversion", "transcribe"):
            self.assertIn("display_status", report_default["modes"][mode])
            self.assertIn("missing_reasons", report_default["modes"][mode])
            self.assertIn("allowed_by_gate", report_default["modes"][mode])
            self.assertTrue(report_default["modes"][mode]["allowed_by_gate"])

        # 2. With Polish-only gate
        gate_polish = {"status": "PASS", "allowed_modes": ["polish"]}
        report_polish = capability_report(registry, runtime_verification=gate_polish)
        self.assertEqual(report_polish["modes"]["polish"]["display_status"], "READY")
        self.assertTrue(report_polish["modes"]["polish"]["allowed_by_gate"])
        self.assertFalse(report_polish["modes"]["voice_conversion"]["allowed_by_gate"])
        self.assertEqual(report_polish["modes"]["voice_conversion"]["status"], "BLOCKED")
        self.assertIn(report_polish["modes"]["voice_conversion"]["display_status"],
                      ("MISSING_COMPONENTS", "USER_IMPORT_REQUIRED"))
        self.assertFalse(report_polish["modes"]["transcribe"]["allowed_by_gate"])
        self.assertEqual(report_polish["modes"]["transcribe"]["status"], "BLOCKED")

        # 3. With failed gate
        gate_fail = {"status": "FAIL"}
        report_fail = capability_report(registry, runtime_verification=gate_fail)
        for mode in ("polish", "voice_conversion", "transcribe"):
            self.assertEqual(report_fail["modes"][mode]["display_status"], "FAILED")
            self.assertEqual(report_fail["modes"][mode]["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
