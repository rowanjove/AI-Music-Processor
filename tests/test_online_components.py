import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.online_components import OnlineComponentError, _InstallTransaction, _install_complete_donor, _install_wheels, install, load_bootstrap_packages, load_sources, plan


class OnlineComponentsTests(unittest.TestCase):
    def test_sources_cover_catalog_and_unverified_assets_are_explicit(self):
        catalog = json.loads((ROOT / "runtime-manifest/components.json").read_text(encoding="utf-8"))
        document, by_id = load_sources(ROOT / "packaging/download-sources.json", catalog=catalog, version="1.0.0-local.20261009")
        self.assertEqual(set(by_id), {item["id"] for item in catalog["components"]})
        self.assertEqual(document["policy"]["transport"], "https-only-with-explicit-hosts")
        for component_id, entry in by_id.items():
            if entry["status"] == "PINNED":
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$", component_id)
            else:
                self.assertTrue(entry["reason"], component_id)
        self.assertEqual(by_id["tool.ffmpeg"]["status"], "PINNED")
        self.assertEqual(by_id["tool.ffprobe"]["status"], "DERIVED")
        bootstrap = load_bootstrap_packages(document)
        self.assertTrue({"python.numpy", "python.scipy", "python.soundfile", "python.cffi", "python.pycparser", "python.mido"}.issubset(bootstrap))

    def test_plan_maps_modes_and_preserves_user_model_boundary(self):
        result = plan(ROOT, ROOT / "packaging/download-sources.json", "voice_conversion")
        self.assertIn("mode.voice_conversion", result["component_ids"])
        self.assertIn("engine.rvc", result["component_ids"])
        _document, sources = load_sources(ROOT / "packaging/download-sources.json", version="1.0.0-local.20261009")
        self.assertEqual(sources["model.rvc.user_scan"]["status"], "USER_OWNED_ONLY")

    def test_polish_plan_contains_real_runtime_bootstrap(self):
        result = plan(ROOT, ROOT / "packaging/download-sources.json", "polish")
        self.assertEqual(result["bootstrap_packages"], ["python.numpy", "python.scipy", "python.soundfile", "python.cffi", "python.pycparser", "python.mido", "python.packaging"])
        self.assertIn("tool.ffmpeg", result["component_ids"])
        self.assertTrue(all(row["source_kind"] in {"component", "bootstrap"} for row in result["rows"]))

    def test_staged_wheel_installs_and_imports_in_isolated_site(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            root.mkdir()
            package = base / "demo_pkg-1.0-py3-none-any.whl"
            files = {
                "demo_pkg/__init__.py": b"VALUE = 42\n",
                "demo_pkg-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0\n",
                "demo_pkg-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                "demo_pkg-1.0.dist-info/RECORD": b"",
            }
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
            python = ROOT / "tools/RVC/RVC-beta-v2-0618/runtime/python.exe"
            if not python.is_file():
                self.skipTest("应用 Python 3.9 runtime 不存在")
            result = _install_wheels(
                root,
                [{"component_id": "test.demo", "artifact": str(package)}],
                [{"component_id": "test.demo", "import_probe": "demo_pkg"}],
                python,
                site_packages=base / "isolated-site",
            )
            self.assertEqual(result["probe"], "passed")
            self.assertTrue((base / "isolated-site" / "demo_pkg" / "__init__.py").is_file())

    def test_wheel_probe_failure_does_not_commit_site_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            root.mkdir()
            package = base / "demo_pkg-1.0-py3-none-any.whl"
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("demo_pkg/__init__.py", b"VALUE = 42\n")
                archive.writestr("demo_pkg-1.0.dist-info/METADATA", b"Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0\n")
                archive.writestr("demo_pkg-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
                archive.writestr("demo_pkg-1.0.dist-info/RECORD", b"")
            site = base / "isolated-site"
            site.mkdir()
            keep = site / "keep.py"
            keep.write_text("before\n", encoding="utf-8")
            python = ROOT / "tools/RVC/RVC-beta-v2-0618/runtime/python.exe"
            if not python.is_file():
                self.skipTest("应用 Python 3.9 runtime 不存在")
            with self.assertRaises(OnlineComponentError):
                _install_wheels(
                    root,
                    [{"component_id": "test.demo", "artifact": str(package)}],
                    [{"component_id": "test.demo", "import_probe": "module_that_does_not_exist"}],
                    python,
                    site_packages=site,
                )
            self.assertEqual(keep.read_text(encoding="utf-8"), "before\n")
            self.assertFalse((site / "demo_pkg").exists())

    def test_install_transaction_restores_existing_file_and_removes_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            existing = root / "runtime" / "bin" / "existing.dll"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"before")
            created = root / "runtime" / "bin" / "created.dll"
            transaction = _InstallTransaction(root)
            transaction.snapshot(existing)
            transaction.snapshot(created)
            existing.write_bytes(b"after")
            created.write_bytes(b"new")
            transaction.rollback()
            self.assertEqual(existing.read_bytes(), b"before")
            self.assertFalse(created.exists())

    def test_complete_donor_installs_only_declared_managed_files_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            (root / "runtime-manifest").mkdir(parents=True)
            (root / "VERSION").write_text("1.0.0-local.20261009\n", encoding="utf-8")
            donor = base / "donor"
            (donor / "core").mkdir(parents=True)
            payload = donor / "core" / "patched.py"
            payload.write_bytes(b"verified source")
            (donor / "release-manifest.json").write_text(json.dumps({
                "version": "1.0.0-local.20261009",
                "files": [{"path": "core/patched.py", "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}],
            }), encoding="utf-8")
            user = root / "models" / "rvc" / "mine.pth"
            user.parent.mkdir(parents=True)
            user.write_bytes(b"preserve")
            stale = root / "tools" / "RVC" / "RVC-beta-v2-0618" / "runtime" / "stale.pyd"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale lightweight host")
            with mock.patch("core.online_components._verify_complete_donor", return_value={"status": "PASS"}):
                result = _install_complete_donor(donor, root)
            self.assertEqual(result["status"], "INSTALLED")
            self.assertEqual(payload.read_bytes(), (root / "core/patched.py").read_bytes())
            self.assertEqual(user.read_bytes(), b"preserve")
            self.assertFalse(stale.exists())
            receipt = root / "runtime-manifest" / "lightweight-install.json"
            self.assertTrue(receipt.is_file())
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["user_data_preserved"], True)

    def test_donor_verification_failure_restores_replaced_and_cleaned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            (root / "runtime-manifest").mkdir(parents=True)
            (root / "VERSION").write_text("1.0.0-local.20261009\n", encoding="utf-8")
            old = root / "core" / "patched.py"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            stale = root / "runtime" / "stale.bin"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale")
            donor = base / "donor"
            payload = donor / "core" / "patched.py"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"new")
            (donor / "release-manifest.json").write_text(json.dumps({
                "version": "1.0.0-local.20261009",
                "files": [{"path": "core/patched.py", "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}],
            }), encoding="utf-8")
            with mock.patch("core.online_components._verify_complete_donor", side_effect=OnlineComponentError("strict verify failed")):
                with self.assertRaises(OnlineComponentError):
                    _install_complete_donor(donor, root)
            self.assertEqual(old.read_bytes(), b"old")
            self.assertEqual(stale.read_bytes(), b"stale")

    def test_donor_rejects_user_data_paths_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            (root / "runtime-manifest").mkdir(parents=True)
            (root / "VERSION").write_text("1.0.0-local.20261009\n", encoding="utf-8")
            donor = base / "donor"
            payload = donor / "models" / "rvc" / "bad.pth"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"must reject")
            (donor / "release-manifest.json").write_text(json.dumps({
                "version": "1.0.0-local.20261009",
                "files": [{"path": "models/rvc/bad.pth", "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}],
            }), encoding="utf-8")
            with self.assertRaises(OnlineComponentError):
                _install_complete_donor(donor, root)
            self.assertFalse((root / "models").exists())

    def test_online_artifact_cache_is_not_reported_as_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            (root / "runtime-manifest").mkdir(parents=True)
            (root / "VERSION").write_text("1.0.0-local.20261009\n", encoding="utf-8")
            (root / "runtime-manifest" / "components.json").write_bytes(
                (ROOT / "runtime-manifest" / "components.json").read_bytes())
            source_manifest = root / "sources.json"
            source_manifest.write_bytes((ROOT / "packaging" / "download-sources.json").read_bytes())
            with mock.patch("core.online_components.download_verified", return_value={"complete": True, "network": False}):
                with self.assertRaises(OnlineComponentError):
                    install(root, source_manifest, "runtime.faiss", cache=root / "cache",
                            receipt_root=root / "runtime-manifest")
            receipt = json.loads((root / "runtime-manifest" / "online-components-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "CACHED_NOT_INSTALLED")

    def test_online_receipt_default_stays_in_external_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            (root / "runtime-manifest").mkdir(parents=True)
            (root / "VERSION").write_text("1.0.0-local.20261009\n", encoding="utf-8")
            (root / "runtime-manifest" / "components.json").write_bytes(
                (ROOT / "runtime-manifest" / "components.json").read_bytes())
            source_manifest = root / "sources.json"
            source_manifest.write_bytes((ROOT / "packaging" / "download-sources.json").read_bytes())
            user_data = base / "user-data"
            with mock.patch.dict(os.environ, {"AI_MUSIC_PROCESSOR_DATA_ROOT": str(user_data)}, clear=False):
                with mock.patch("core.online_components.download_verified", return_value={"complete": True, "network": False}):
                    with self.assertRaises(OnlineComponentError):
                        install(root, source_manifest, "runtime.faiss", cache=root / "cache")
            self.assertTrue((user_data / "logs" / "online-components-receipt.json").is_file())
            self.assertFalse((root / "runtime-manifest" / "online-components-receipt.json").exists())

    def test_online_polish_refuses_to_downgrade_strict_full_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            (root / "runtime-manifest").mkdir(parents=True)
            (root / "VERSION").write_text("1.0.0-local.20261009\n", encoding="utf-8")
            (root / "runtime-manifest" / "runtime.json").write_text("{}\n", encoding="utf-8")
            source_manifest = root / "sources.json"
            source_manifest.write_bytes((ROOT / "packaging" / "download-sources.json").read_bytes())
            with self.assertRaisesRegex(OnlineComponentError, "strict release"):
                install(root, source_manifest, "polish", cache=root / "cache")

    def test_ampkg_is_delegated_only_after_a_trusted_inventory_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            (root / "runtime-manifest").mkdir(parents=True)
            (root / "runtime-manifest" / "inventory.json").write_text(json.dumps({"files": []}), encoding="utf-8")
            package = root.parent / "repair.ampkg"
            package.write_bytes(b"test placeholder")
            with mock.patch("core.online_components.install_offline_package", return_value={"status": "NO_CHANGE", "installed": []}) as installer:
                result = _install_complete_donor(package, root)
            installer.assert_called_once_with(str(package.resolve()), str(root))
            self.assertEqual(result["status"], "NO_CHANGE")

    def test_plan_rows_include_human_status_and_status_descriptions(self):
        result = plan(ROOT, ROOT / "packaging/download-sources.json", "all")
        self.assertTrue(result["rows"])
        for row in result["rows"]:
            self.assertIn("human_status", row)
            self.assertTrue(len(row["human_status"]) > 0)
        ffmpeg_row = next(r for r in result["rows"] if r["component_id"] == "tool.ffmpeg")
        self.assertIn("可官方在线下载", ffmpeg_row["human_status"])
        unavailable_row = next(r for r in result["rows"] if r["component_id"] == "engine.rvc")
        self.assertIn("缺少可信公开来源", unavailable_row["human_status"])


if __name__ == "__main__":
    unittest.main()
