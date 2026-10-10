import csv
import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import freeze_runtime as freeze


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.resolve().relative_to(other.resolve())
        return True
    except ValueError:
        return False


class RuntimeManifestTests(unittest.TestCase):
    def _write(self, root, relative, content):
        root = Path(root).resolve(strict=True)
        relative = Path(relative)
        if not root.is_absolute() or (not relative.is_absolute() and ".." in relative.parts):
            raise AssertionError("fixture writes require a bounded absolute temporary root")
        path = relative if relative.is_absolute() else root / relative
        path = path.resolve()
        if not _is_relative_to(path, root):
            raise AssertionError("fixture output escaped its temporary root")
        if not _is_relative_to(path.parent.resolve(), root):
            raise AssertionError("fixture output escaped its temporary root")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def _distribution(self, root, site_root, name, version, module_name):
        module = self._write(root, site_root / module_name / "__init__.py", ("ORIGIN = %r\n" % site_root.as_posix()).encode())
        info_root = root / site_root / (name.replace("-", "_") + "-" + version + ".dist-info")
        metadata = self._write(root, info_root / "METADATA", "Metadata-Version: 2.1\nName: %s\nVersion: %s\n\n" % (name, version))
        top_level = self._write(root, info_root / "top_level.txt", module_name + "\n")
        record = info_root / "RECORD"
        if not _is_relative_to(record.resolve(), root.resolve()):
            raise AssertionError("fixture metadata output escaped its temporary root")
        record.parent.mkdir(parents=True, exist_ok=True)
        with record.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            absolute_site_root = root / site_root
            for path in (module, metadata, top_level):
                writer.writerow([path.relative_to(absolute_site_root).as_posix(), "", ""])
            writer.writerow([(root / record).relative_to(absolute_site_root).as_posix(), "", ""])

    def _fixture(self, root):
        addon_root = Path("runtime/addons")
        site_root = Path("tools/RVC/RVC-beta-v2-0618/runtime/lib/site-packages")
        self._distribution(root, addon_root, "demo-pkg", "1.0", "demo_pkg")
        self._distribution(root, site_root, "demo-pkg", "2.0", "demo_pkg")
        self._write(root, addon_root / "orphan.pyc", b"sourceless bytecode is retained")
        self._write(root, addon_root / "__pycache__" / "generated.pyc", b"rebuildable cache")
        for relative, content in (
            ("runtime/bin/ffmpeg.exe", b"ffmpeg fixture"),
            ("runtime/bin/ffprobe.exe", b"ffprobe fixture"),
            ("runtime/addons/basic_pitch/saved_models/icassp_2022/nmp.onnx", b"basic pitch model"),
            ("tools/RVC/RVC-beta-v2-0618/rmvpe.pt", b"rmvpe model fixture"),
            ("tools/UVR5/models/MDX_Net_Models/Kim_Vocal_2.onnx", b"Kim model fixture"),
            ("tools/UVR5/models/MDX_Net_Models/UVR-MDX-NET-Inst_HQ_3.onnx", b"Inst model fixture"),
            ("tools/UVR5/models/MDX_Net_Models/model_data/model_data.json", b"{}"),
            ("tools/RVC/RVC-beta-v2-0618/runtime/python39.dll", b"python runtime"),
            ("tools/RVC/RVC-beta-v2-0618/ffmpeg.exe", b"ffmpeg fixture"),
            ("tools/RVC/RVC-beta-v2-0618/ffprobe.exe", b"ffprobe fixture"),
            ("core/worker.py", b"worker source"),
            ("AI_Music_Processor.pyw", b"entry point"),
            ("process_music.py", b"CLI entry point"),
            ("tools/RVC/RVC-beta-v2-0618/configs/config.json", b"{}"),
            ("tools/RVC/RVC-beta-v2-0618/infer_pack/models.py", b"rvc inference"),
            ("tools/RVC/RVC-beta-v2-0618/uvr5_pack/utils.py", b"uvr inference"),
            ("tools/RVC/RVC-beta-v2-0618/config.py", b"vendor config"),
            ("tools/RVC/RVC-beta-v2-0618/infer_uvr5.py", b"uvr entry"),
            ("tools/RVC/RVC-beta-v2-0618/my_utils.py", b"audio helpers"),
            ("tools/RVC/RVC-beta-v2-0618/vc_infer_pipeline.py", b"rvc pipeline"),
            ("tools/RVC/RVC-beta-v2-0618/hubert_base.pt", b"hubert model"),
            ("tools/RVC/RVC-beta-v2-0618/weights/default.pth", b"rvc model"),
            ("tools/RVC/RVC-beta-v2-0618/logs/default/model.index", b"rvc index"),
            ("tools/RVC/RVC-beta-v2-0618/uvr5_weights/HP2_all_vocals.pth", b"uvr model"),
            ("tools/UVR5/models/Demucs_Models/v3_v4_repo/checkpoint.th", b"demucs model"),
            ("tools/UVR5/models/Demucs_Models/v3_v4_repo/config.yaml", b"demucs config"),
        ):
            self._write(root, Path(relative), content)
        components = {
            "schema_version": 1,
            "catalog_kind": "installed-workspace-description",
            "size_policy": "probe bytes only",
            "components": [{
                "id": "engine.fixture",
                "name": "fixture engine",
                "kind": "engine",
                "ownership": "managed",
                "required": True,
                "implemented": True,
                "version": "fixture",
                "capabilities": ["engine.fixture"],
                "modes": [],
                "dependencies": [],
                "probes": [{"type": "relative_file", "path": "core/worker.py", "role": "required"}],
                "license_status": "UNKNOWN",
                "distribution_status": "UNKNOWN",
            }],
        }
        self._write(root, Path("runtime-manifest/components.json"), json.dumps(components))
        return site_root

    def _inspect(self, root, modules=("demo_pkg",)):
        return freeze.inspect_python(root, sys.executable, module_names=modules, allow_external=True)

    def _build(self, root, python_info=None):
        return freeze.build_snapshot(root, Path("runtime-manifest"), python_info=python_info or self._inspect(root))

    def _load_snapshot(self, root):
        base = Path(root) / "runtime-manifest"
        names = ("runtime.json", "inventory.json", "packages.lock.json", "vendor.json", "models.json", "components.json")
        return [freeze.json.loads((base / name).read_text(encoding="utf-8")) for name in names]

    def test_addons_precede_runtime_site_packages_and_metadata_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root)
            details = self._inspect(root)
            packages = [item for item in details["packages"] if item["normalized_name"] == "demo-pkg"]
            self.assertEqual([item["version"] for item in packages], ["1.0", "2.0"])
            self.assertEqual(packages[0]["site_root"], "runtime/addons")
            self.assertEqual(details["module_resolution"]["demo_pkg"]["origin"],
                             "runtime/addons/demo_pkg/__init__.py")
            self.assertTrue(all(package["metadata_files"] for package in packages))

    def test_freeze_digest_is_stable_and_content_changes_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root)
            first = self._build(root)
            first_summary_bytes = (root / "runtime-manifest" / "runtime.json").read_bytes()
            second = self._build(root)
            self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
            self.assertEqual((root / "runtime-manifest" / "runtime.json").read_bytes(), first_summary_bytes,
                             "identical snapshots should preserve the exact summary timestamp")

            inventory = freeze.json.loads((root / "runtime-manifest" / "inventory.json").read_text(encoding="utf-8"))
            paths = {record["path"] for record in inventory["files"]}
            self.assertIn("runtime/addons/orphan.pyc", paths)
            records = {record["path"]: record for record in inventory["files"]}
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                bundled = records["runtime/bin/" + name]
                vendor = records["tools/RVC/RVC-beta-v2-0618/" + name]
                self.assertEqual((vendor["size"], vendor["sha256"]),
                                 (bundled["size"], bundled["sha256"]))
            self.assertFalse(any("__pycache__" in path for path in paths))

            binary = root / "runtime/bin/ffmpeg.exe"
            binary.write_bytes(b"tampered binary")
            changed_binary = self._build(root)
            self.assertNotEqual(first["snapshot_sha256"], changed_binary["snapshot_sha256"])

            model = root / "tools/RVC/RVC-beta-v2-0618/weights/default.pth"
            model.write_bytes(b"modified model")
            changed_model = self._build(root)
            self.assertNotEqual(changed_binary["snapshot_sha256"], changed_model["snapshot_sha256"])

            metadata = root / "runtime/addons/demo_pkg-1.0.dist-info/METADATA"
            metadata.write_text("Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.1\n\n", encoding="utf-8")
            changed_package = self._build(root)
            self.assertNotEqual(changed_model["snapshot_sha256"], changed_package["snapshot_sha256"])
            lock = freeze.json.loads((root / "runtime-manifest" / "packages.lock.json").read_text(encoding="utf-8"))
            self.assertEqual(lock["distributions"][0]["version"], "1.1")

    def test_schema_rejects_missing_wrong_duplicate_and_escaping_records(self):
        good = {"path": "runtime/bin/tool.exe", "size": 2, "sha256": hashlib.sha256(b"ok").hexdigest()}
        self.assertTrue(freeze.validate_file_records([good]))
        for bad in (
            {"path": "runtime/bin/tool.exe", "size": 2},
            {"path": "runtime/bin/tool.exe", "size": True, "sha256": good["sha256"]},
            {"path": "../outside.exe", "size": 2, "sha256": good["sha256"]},
            {"path": "C:/outside.exe", "size": 2, "sha256": good["sha256"]},
        ):
            with self.subTest(record=bad), self.assertRaises(ValueError):
                freeze.validate_file_records([bad])
        duplicate = dict(good, path="RUNTIME/BIN/TOOL.EXE")
        with self.assertRaisesRegex(ValueError, "duplicate Windows paths"):
            freeze.validate_file_records([good, duplicate])

    def test_snapshot_validation_rejects_dangling_model_and_role_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root)
            self._build(root)
            summary, inventory, packages, vendor, models, components = self._load_snapshot(root)

            bad_model = copy.deepcopy(models)
            bad_model["assets"][0]["files"][0]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "model file differs from inventory"):
                freeze.validate_snapshot(summary, inventory, packages, vendor, bad_model, components)

            bad_inventory = copy.deepcopy(inventory)
            bad_inventory["files"][0]["roles"] = []
            with self.assertRaisesRegex(ValueError, "roles must be a non-empty array"):
                freeze.validate_snapshot(summary, bad_inventory, packages, vendor, models, components)

    def test_reparse_point_is_rejected_during_inventory_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "runtime/addons/linked/payload.bin", b"data")
            original = freeze._is_reparse
            with mock.patch.object(freeze, "_is_reparse",
                                   side_effect=lambda path: Path(path).name == "linked" or original(path)):
                with self.assertRaisesRegex(ValueError, "reparse point"):
                    list(freeze._walk_files(root, root / "runtime"))

    def test_file_change_between_stat_and_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write(root, "runtime/tool.bin", b"abc")
            signatures = [(3, 1, 1, 1), (3, 2, 2, 1)]
            with mock.patch.object(freeze, "_stat_signature", side_effect=signatures):
                with self.assertRaisesRegex(RuntimeError, "changed while it was being hashed"):
                    freeze._hash_stable_file(root, path)


if __name__ == "__main__":
    unittest.main()
