import base64
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.component_package import (
    ComponentPackageCancelled,
    ComponentPackageError,
    UNSIGNED_STATUS,
    build_component_package,
    extract_component_package,
    inspect_component_package,
)


class ComponentPackageTests(unittest.TestCase):
    def test_archive_installer_repairs_corruption_and_preserves_package(self):
        from core.install_platform import install_offline_package
        result=self._package()
        archive=Path(result['package_path'])
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        live=self.root/'core/pipeline.py'
        original=live.read_bytes();live.write_bytes(b'corrupt live file')
        repaired=install_offline_package(archive,self.root)
        self.assertEqual(repaired['status'],'INSTALLED')
        self.assertTrue(repaired['restart_required'])
        self.assertEqual(live.read_bytes(),original)
        self.assertTrue(Path(repaired['component_package_receipt']).is_file())
        self.assertFalse(repaired['extracted_payload_retained'])
        self.assertFalse(list((self.root/'temp').glob('ampkg-*')))
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),digest)
        with self.assertRaises(RuntimeError):install_offline_package(archive,self.root,in_use=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ampkg-test-")
        self.root = Path(self.temp.name) / "app"
        self.root.mkdir()
        for relative, content in {
            "VERSION": "1.2.3-test\n",
            "AI_Music_Processor.pyw": "app",
            "process_music.py": "process",
            "core/pipeline.py": "pipeline",
            "tools/runtime/python.exe": "runtime",
            "ignored.txt": "outside managed component",
        }.items():
            path = self.root / Path(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        catalog = {
            "schema_version": 1,
            "catalog_kind": "installed-workspace-description",
            "size_policy": "test",
            "components": [
                {
                    "id": "app.core", "name": "Core", "kind": "application",
                    "ownership": "managed", "required": True, "implemented": True,
                    "capabilities": ["app.launch"], "modes": ["polish"],
                    "dependencies": [], "probes": [
                        {"type": "relative_file", "path": "AI_Music_Processor.pyw", "role": "required"},
                        {"type": "relative_file", "path": "process_music.py", "role": "required"},
                        {"type": "relative_file", "path": "core/pipeline.py", "role": "required"},
                    ], "license_status": "UNKNOWN", "distribution_status": "UNKNOWN",
                },
                {
                    "id": "runtime.python", "name": "Runtime", "kind": "runtime",
                    "ownership": "managed", "required": True, "implemented": True,
                    "capabilities": ["runtime.python"], "modes": ["polish"],
                    "dependencies": [], "probes": [
                        {"type": "relative_file", "path": "tools/runtime/python.exe", "role": "required"},
                    ], "license_status": "UNKNOWN", "distribution_status": "UNKNOWN",
                },
                {
                    "id": "mode.polish", "name": "Polish", "kind": "mode_pack",
                    "ownership": "managed", "required": True, "implemented": True,
                    "capabilities": ["mode.polish"], "modes": ["polish"],
                    "dependencies": ["app.core", "runtime.python"], "probes": [],
                    "license_status": "UNKNOWN", "distribution_status": "UNKNOWN",
                },
            ],
        }
        catalog_path = self.root / "runtime-manifest/components.json"
        catalog_path.parent.mkdir(parents=True)
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        records = []
        for relative in ("VERSION", "AI_Music_Processor.pyw", "process_music.py", "core/pipeline.py",
                         "tools/runtime/python.exe"):
            path = self.root / Path(*relative.split("/"))
            records.append({"path": relative, "size": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        (self.root / "runtime-manifest/inventory.json").write_text(
            json.dumps({"schema_version": 1, "files": records}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _package(self, name="component.ampkg", **kwargs):
        return build_component_package(
            self.root, ["mode.polish"], Path(self.temp.name) / name,
            package_id="test-package", **kwargs,
        )

    def test_real_zip_round_trip_and_installable_layout(self):
        result = self._package()
        package = Path(result["package_path"])
        self.assertTrue(zipfile.is_zipfile(package))
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(archive.namelist()[0], "manifest.json")
            self.assertIn("payload/core/pipeline.py", archive.namelist())
            self.assertNotIn("payload/ignored.txt", archive.namelist())
        inspected = inspect_component_package(package, current_root=self.root)
        self.assertEqual(inspected["status"], "VERIFIED")
        self.assertEqual(inspected["signature_status"], UNSIGNED_STATUS)
        extracted = extract_component_package(
            package, current_root=self.root,
            user_temp_root=Path(self.temp.name) / "userdata" / "temp",
        )
        folder = Path(extracted["folder"])
        self.assertTrue((folder / "manifest.json").is_file())
        self.assertTrue((folder / "ampkg-receipt.json").is_file())
        self.assertEqual((folder / "core/pipeline.py").read_text(), "pipeline")
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["files"][0]["path"], "AI_Music_Processor.pyw")

    def test_payload_tamper_is_rejected(self):
        result = self._package()
        tampered = Path(self.temp.name) / "tampered.ampkg"
        with zipfile.ZipFile(result["package_path"]) as source, zipfile.ZipFile(tampered, "w") as target:
            for info in source.infolist():
                data = source.read(info)
                if info.filename == "payload/core/pipeline.py":
                    data = b"tampered"
                target.writestr(info, data)
        with self.assertRaisesRegex(ComponentPackageError, "SHA-256"):
            inspect_component_package(tampered)

    def test_zip_slip_duplicate_and_unknown_members_are_rejected(self):
        def write_bad(path, manifest, members):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                for name, data in members:
                    archive.writestr(name, data)

        base = {
            "schema_version": 1, "package_kind": "ampkg", "package_id": "bad",
            "version": "1", "component_ids": ["app.core"], "dependencies": [],
            "app_compatibility": {"catalog_schema_version": 1},
            "license_status": "UNKNOWN", "distribution_status": "UNKNOWN",
            "signature": {"status": UNSIGNED_STATUS},
            "files": [{"path": "AI_Music_Processor.pyw", "size": 1, "sha256": "0" * 64}],
        }
        slip = Path(self.temp.name) / "slip.ampkg"
        write_bad(slip, base, [("payload/../escape.txt", b"x")])
        with self.assertRaises(ComponentPackageError):
            inspect_component_package(slip)

        unknown = Path(self.temp.name) / "unknown.ampkg"
        write_bad(unknown, base, [("payload/AI_Music_Processor.pyw", b"x"),
                                  ("payload/extra.txt", b"x")])
        with self.assertRaises(ComponentPackageError):
            inspect_component_package(unknown)

        duplicate = Path(self.temp.name) / "duplicate.ampkg"
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("manifest.json", b"{}")
            archive.writestr("payload/a.txt", b"a")
            archive.writestr("payload/A.txt", b"a")
        with self.assertRaisesRegex(ComponentPackageError, "重复|大小写"):
            inspect_component_package(duplicate)

    def test_dependency_and_current_inventory_are_frozen(self):
        result = self._package()
        manifest = result["manifest"]
        self.assertEqual(manifest["component_ids"], ["mode.polish"])
        self.assertEqual(manifest["dependencies"], ["app.core", "runtime.python"])
        (self.root / "core/pipeline.py").write_text("changed", encoding="utf-8")
        checked = inspect_component_package(result["package_path"], current_root=self.root)
        self.assertEqual(checked["inventory_status"], "MATCHED")
        extracted = extract_component_package(
            result["package_path"], current_root=self.root,
            user_temp_root=Path(self.temp.name) / "userdata" / "temp",
        )
        from core.install_platform import install_offline_package
        installed = install_offline_package(extracted["folder"], self.root)
        self.assertEqual(installed["status"], "INSTALLED")
        self.assertEqual((self.root / "core/pipeline.py").read_text(), "pipeline")

    def test_cancel_does_not_publish_partial_zip(self):
        output = Path(self.temp.name) / "cancelled.ampkg"
        with self.assertRaises(ComponentPackageCancelled):
            build_component_package(self.root, ["mode.polish"], output,
                                    package_id="cancel", cancel=lambda: True)
        self.assertFalse(output.exists())
        self.assertFalse(any(path.name.startswith(".cancelled") for path in output.parent.iterdir()))

    def test_signed_package_requires_explicit_trusted_key_and_rejects_unknown_key(self):
        try:
            from Crypto.PublicKey import RSA
        except ImportError:
            self.skipTest("PyCryptodome unavailable")
        key = RSA.generate(2048)
        signed = self._package(signing_key=key)
        package = Path(signed["package_path"])
        with self.assertRaisesRegex(ComponentPackageError, "trusted_public_keys"):
            inspect_component_package(package)
        public = key.publickey()
        checked = inspect_component_package(package, current_root=self.root,
                                            trusted_public_keys=[public])
        self.assertEqual(checked["signature_status"], "SIGNED_RSA_SHA256")
        other = RSA.generate(2048).publickey()
        with self.assertRaisesRegex(ComponentPackageError, "key_id"):
            inspect_component_package(package, trusted_public_keys=[other])

    def test_only_userdata_temp_can_receive_extraction(self):
        result = self._package()
        outside = Path(self.temp.name) / "outside"
        with self.assertRaisesRegex(ComponentPackageError, "userdata-owned"):
            extract_component_package(result["package_path"], outside,
                                       current_root=self.root,
                                       user_temp_root=Path(self.temp.name) / "userdata" / "temp")

    def test_broad_rvc_probe_does_not_export_user_voice_files(self):
        code = self.root / "tools/RVC/vc_infer_pipeline.py"
        user_model = self.root / "tools/RVC/weights/imported-user.pth"
        code.parent.mkdir(parents=True, exist_ok=True)
        (user_model.parent).mkdir(parents=True, exist_ok=True)
        code.write_bytes(b"engine")
        user_model.write_bytes(b"user voice model")
        catalog_path = self.root / "runtime-manifest/components.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["components"].extend([
            {
                "id": "engine.rvc", "name": "RVC", "kind": "engine",
                "ownership": "managed", "required": True, "implemented": True,
                "capabilities": ["voice.convert"], "modes": ["voice_conversion"],
                "dependencies": [], "probes": [
                    {"type": "relative_dir", "path": "tools/RVC", "role": "required"},
                ], "license_status": "UNKNOWN", "distribution_status": "UNKNOWN",
            },
            {
                "id": "model.rvc.user_scan", "name": "User RVC", "kind": "model_scan",
                "ownership": "user", "required": False, "implemented": True,
                "capabilities": ["voice.user_models"], "modes": ["voice_conversion"],
                "dependencies": [], "probes": [{
                    "type": "user_rvc_models", "weights_dir": "tools/RVC/weights",
                    "logs_dir": "tools/RVC/logs", "user_root": "models/rvc",
                    "managed_files": ["built-in.pth"],
                }], "license_status": "UNKNOWN", "distribution_status": "UNKNOWN",
            },
        ])
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        inventory_path = self.root / "runtime-manifest/inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        for path in (code, user_model):
            relative = path.relative_to(self.root).as_posix()
            inventory["files"].append({"path": relative, "size": path.stat().st_size,
                                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
        package = build_component_package(
            self.root, ["engine.rvc"], Path(self.temp.name) / "rvc.ampkg",
            package_id="rvc-package",
        )
        paths = {item["path"] for item in package["manifest"]["files"]}
        self.assertIn("tools/RVC/vc_infer_pipeline.py", paths)
        self.assertNotIn("tools/RVC/weights/imported-user.pth", paths)

    def test_payload_must_belong_to_selected_component_probe(self):
        result = self._package()
        extra = self.root / "inventory-only.txt"
        extra.write_bytes(b"inventory but not a component probe")
        inventory_path = self.root / "runtime-manifest/inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["files"].append({"path": "inventory-only.txt", "size": extra.stat().st_size,
                                    "sha256": hashlib.sha256(extra.read_bytes()).hexdigest()})
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

        package = Path(result["package_path"])
        altered = Path(self.temp.name) / "unauthorized.ampkg"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(altered, "w") as target:
            manifest = json.loads(source.read("manifest.json"))
            record = {"path": "inventory-only.txt", "size": extra.stat().st_size,
                      "sha256": hashlib.sha256(extra.read_bytes()).hexdigest()}
            manifest["files"].append(record)
            manifest["files"].sort(key=lambda item: item["path"].casefold())
            target.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False,
                                                          sort_keys=True, separators=(",", ":")))
            for info in source.infolist():
                if info.filename != "manifest.json":
                    target.writestr(info, source.read(info))
            target.writestr("payload/inventory-only.txt", extra.read_bytes())
        with self.assertRaisesRegex(ComponentPackageError, "probe"):
            inspect_component_package(altered, current_root=self.root)

    def test_atomic_package_replacement_between_checks_is_rejected(self):
        result = self._package()
        package = Path(result["package_path"])
        replacement = Path(self.temp.name) / "replacement.ampkg"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(replacement, "w") as target:
            for info in source.infolist():
                data = source.read(info)
                if info.filename == "payload/core/pipeline.py":
                    data = b"replace!"
                target.writestr(info, data)
        import core.component_package as package_module
        original_reader = package_module._read_zip_manifest
        calls = [0]

        def replacing_reader(path):
            calls[0] += 1
            if calls[0] == 2:
                os.replace(replacement, path)
            return original_reader(path)

        package_module._read_zip_manifest = replacing_reader
        try:
            with self.assertRaisesRegex(ComponentPackageError, "替换"):
                extract_component_package(
                    package, current_root=self.root,
                    user_temp_root=Path(self.temp.name) / "userdata" / "temp",
                )
        finally:
            package_module._read_zip_manifest = original_reader


if __name__ == "__main__":
    unittest.main()
