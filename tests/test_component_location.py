import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from core.component_location import (
    ComponentLocationError,
    install_verified_bundle,
    resolve_editor_location,
    save_editor_location,
)
from core.paths import PORTABLE_RELEASE_CONTENT
from scripts import build_portable


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class ComponentLocationTests(unittest.TestCase):
    def _bundle(self, root):
        root = Path(root)
        files = {
            "VERSION": b"1.0.0\n",
            "core/entry.py": b"print('bundle')\n",
            "runtime/bin/ffmpeg.exe": b"ffmpeg fixture\n",
            "runtime-manifest/inventory.json": b'{"files": []}\n',
        }
        for relative, payload in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (root / "PORTABLE_RELEASE").write_bytes(PORTABLE_RELEASE_CONTENT.encode("ascii"))
        records = []
        for relative in sorted(files):
            payload = root / relative
            records.append({"path": relative, "size": payload.stat().st_size,
                            "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()})
        manifest = {"schema_version": 1, "file_count": len(records), "files": records,
                    "file_inventory_sha256": _digest(records),
                    "source": {"kind": "test-fixture"}}
        (root / "release-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        return files

    def _pe(self, path, size=256):
        data = bytearray(max(256, size))
        data[:2] = b"MZ"
        data[0x3C:0x40] = (0x80).to_bytes(4, "little")
        data[0x80:0x84] = b"PE\0\0"
        Path(path).write_bytes(data)

    def test_install_verifies_manifest_and_publishes_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "portable"
            source.mkdir()
            files = self._bundle(source)
            target = root / "安装目标"
            events = []
            result = install_verified_bundle(source, target, events.append)
            self.assertEqual(result["status"], "INSTALLED")
            self.assertTrue(result["atomic"])
            self.assertFalse(result["user_data_copied"])
            self.assertEqual(result["files"], len(files))
            self.assertTrue((target / "release-manifest.json").is_file())
            self.assertTrue((target / "PORTABLE_RELEASE").is_file())
            self.assertEqual((source / "core/entry.py").read_bytes(), (target / "core/entry.py").read_bytes())
            self.assertEqual(events[0]["event"], "start")
            self.assertEqual(events[-1]["event"], "complete")
            with self.assertRaises(ComponentLocationError):
                install_verified_bundle(source, target)

            nested_target = root / "new-parent" / "child" / "portable"
            result = install_verified_bundle(source, nested_target)
            self.assertEqual(result["target_root"], str(nested_target))
            self.assertTrue((nested_target / "VERSION").is_file())

    def test_real_builder_manifest_includes_marker_and_install_preserves_marker_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "builder-portable"
            (source / "core").mkdir(parents=True)
            (source / "runtime-manifest").mkdir()
            (source / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (source / "core/entry.py").write_text("entry\n", encoding="utf-8")
            (source / "PORTABLE_RELEASE").write_bytes(PORTABLE_RELEASE_CONTENT.encode("ascii"))
            (source / "runtime-manifest/components.json").write_text(
                json.dumps({"components": []}), encoding="utf-8")
            runtime_summary = {
                "snapshot_sha256": "a" * 64,
                "artifacts": {"inventory": {"file_count": 0, "sha256": "b" * 64}},
            }
            manifest = build_portable.write_release_manifest(
                source, source, runtime_summary,
                {"path": "csc.exe", "sha256": "c" * 64, "version": "fixture"})
            marker_record = next(item for item in manifest["files"] if item["path"] == "PORTABLE_RELEASE")
            self.assertEqual(marker_record["sha256"], hashlib.sha256(
                (source / "PORTABLE_RELEASE").read_bytes()).hexdigest())
            target = root / "installed"
            install_verified_bundle(source, target)
            self.assertEqual((target / "PORTABLE_RELEASE").read_bytes(),
                             (source / "PORTABLE_RELEASE").read_bytes())

    def test_legacy_manifest_without_marker_row_preserves_standard_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy"
            source.mkdir()
            self._bundle(source)
            marker_bytes = (PORTABLE_RELEASE_CONTENT + "\r\n").encode("ascii")
            (source / "PORTABLE_RELEASE").write_bytes(marker_bytes)
            target = root / "installed"
            install_verified_bundle(source, target)
            self.assertEqual((target / "PORTABLE_RELEASE").read_bytes(), marker_bytes)

    def test_tamper_traversal_and_user_data_are_rejected_without_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "portable"
            source.mkdir()
            self._bundle(source)
            (source / "core/entry.py").write_bytes(b"tampered\n")
            target = root / "target"
            with self.assertRaises(ComponentLocationError):
                install_verified_bundle(source, target)
            self.assertFalse(target.exists())

            source = root / "portable-user-data"
            source.mkdir()
            self._bundle(source)
            user_file = source / "config/app.json"
            user_file.parent.mkdir()
            user_file.write_text("user", encoding="utf-8")
            with self.assertRaises(ComponentLocationError):
                install_verified_bundle(source, root / "target-user-data")
            self.assertFalse((root / "target-user-data").exists())

            source = root / "portable-traversal"
            source.mkdir()
            self._bundle(source)
            manifest_path = source / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["path"] = "../outside.bin"
            manifest["file_inventory_sha256"] = _digest(manifest["files"])
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ComponentLocationError):
                install_verified_bundle(source, root / "target-traversal")

    def test_source_and_target_nested_and_reparse_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "portable"
            source.mkdir()
            self._bundle(source)
            with self.assertRaises(ComponentLocationError):
                install_verified_bundle(source, source / "child")
            with self.assertRaises(ComponentLocationError):
                install_verified_bundle(source / "child", source)
            outside = root / "outside"
            outside.mkdir()
            link = root / "portable-link"
            try:
                os.symlink(str(outside), str(link), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink unavailable")
            try:
                with self.assertRaises(ComponentLocationError):
                    install_verified_bundle(link, root / "target-link")
            finally:
                link.unlink()

    def test_editor_locations_are_explicit_pe_paths_and_preserve_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            editor = root / "MuseScore.exe"
            self._pe(editor)
            result = save_editor_location(config, "musescore", editor)
            self.assertEqual(result["path"], str(editor))
            settings = config / "external-editors.json"
            payload = json.loads(settings.read_text(encoding="utf-8"))
            payload["unrelated"] = {"preserve": True}
            settings.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(resolve_editor_location(config, "musescore", "default.exe"), str(editor))
            self.assertTrue(json.loads(settings.read_text(encoding="utf-8"))["unrelated"]["preserve"])
            self.assertEqual(resolve_editor_location(config, "audacity", "default.exe"), "default.exe")
            with self.assertRaises(ComponentLocationError):
                save_editor_location(config, "rvc", editor)
            with self.assertRaises(ComponentLocationError):
                save_editor_location(config, "musescore", root / "plain.txt")

    def test_corrupt_settings_and_changed_editor_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            editor = root / "LMMS.exe"
            self._pe(editor)
            save_editor_location(config, "lmms", editor)
            editor.write_bytes(b"changed")
            with self.assertRaises(ComponentLocationError):
                resolve_editor_location(config, "lmms", "default.exe")
            (config / "external-editors.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(ComponentLocationError):
                resolve_editor_location(config, "lmms", "default.exe")


if __name__ == "__main__":
    unittest.main()
