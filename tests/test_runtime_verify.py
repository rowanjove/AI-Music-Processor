import hashlib
import json
import os
import platform
import runpy
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
import builtins
import contextlib
import io
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core import runtime_verify as verify
from core.paths import PORTABLE_RELEASE_CONTENT, PORTABLE_RELEASE_MARKER, RELEASE_MANIFEST


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class RuntimeVerifyTests(unittest.TestCase):
    def _root(self, directory):
        root = Path(directory) / "app"
        root.mkdir()
        return root

    def _write(self, root, relative, content):
        path = Path(root, *relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _fixture(self, root):
        root = Path(root).resolve()
        (root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding="ascii")
        source_files = {
            "runtime/python.exe": b"python fixture marker",
            "runtime/bin/ffmpeg.exe": b"ffmpeg fixture marker",
            "runtime/bin/ffprobe.exe": b"ffprobe fixture marker",
            "tools/RVC/RVC-beta-v2-0618/ffmpeg.exe": b"ffmpeg fixture marker",
            "tools/RVC/RVC-beta-v2-0618/ffprobe.exe": b"ffprobe fixture marker",
            "runtime/addons/demo_pkg/__init__.py": b"VALUE = 7\n",
            "runtime/addons/demo_pkg-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0\n\n",
            "core/fixture.py": b"FIXTURE = True\n",
            "AI_Music_Processor.pyw": b"# fixture entry\n",
            "process_music.py": b"# fixture CLI entry\n",
        }
        module_resolution = {
            "demo_pkg": {"status": "FOUND", "origin": "runtime/addons/demo_pkg/__init__.py",
                         "locations": ["runtime/addons/demo_pkg"]}
        }
        for name in verify.REQUIRED_MODULES:
            relative = "runtime/addons/" + name + ".py"
            if name == "torch":
                source_files[relative] = (b"class _Cuda:\n"
                                          b"    @staticmethod\n"
                                          b"    def is_available(): return False\n"
                                          b"cuda = _Cuda()\n")
            else:
                source_files[relative] = b"FIXTURE_IMPORT = True\n"
            module_resolution[name] = {"status": "FOUND", "origin": relative,
                                       "locations": [relative.rsplit("/", 1)[0]]}
        records = []
        for relative, content in source_files.items():
            self._write(root, relative, content)
            records.append({"path": relative, "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(), "roles": ["fixture"]})
        records.sort(key=lambda item: (item["path"].casefold(), item["path"]))
        metadata_record = next({key: item[key] for key in ("path", "size", "sha256")}
                              for item in records if item["path"].endswith("/METADATA"))
        version = platform.python_version()
        version_full = sys.version
        machine = platform.machine()
        python_rel = "runtime/python.exe"
        runtime_python = {
            "implementation": platform.python_implementation(), "version": version,
            "version_full": version_full, "machine": machine, "bits": struct.calcsize("P") * 8,
            "executable": python_rel, "prefix": "runtime", "base_prefix": "runtime",
            "sys_path": ["runtime/addons"], "search_roots": ["runtime/addons"],
        }
        distributions = [{
            "name": "demo-pkg", "normalized_name": "demo-pkg", "version": "1.0", "search_order": 0,
            "site_root": "runtime/addons", "metadata_root": "runtime/addons/demo_pkg-1.0.dist-info",
            "metadata_files": [metadata_record], "requires_dist": [],
        }]
        inventory = {"schema_version": 1, "files": records,
                     "cache_policy": {"excluded_directory": "__pycache__",
                                      "sourceless_bytecode": "included outside __pycache__"}}
        packages = {
            "schema_version": 1, "runtime_python": runtime_python,
            "search_order": ["runtime/addons"],
            "module_resolution": module_resolution,
            "distributions": distributions,
            "rebuild_claim": "snapshot of the current installed bundle; no network pip rebuild was verified",
        }
        vendor = {"schema_version": 1, "source_status": "UNKNOWN", "license_status": "UNKNOWN",
                  "distribution_status": "UNKNOWN", "roots": [
                      {"id": "fixture", "path": "core/fixture.py", "files": ["core/fixture.py"]},
                      {"id": "rvc-ffmpeg.exe", "path": "tools/RVC/RVC-beta-v2-0618/ffmpeg.exe",
                       "files": ["tools/RVC/RVC-beta-v2-0618/ffmpeg.exe"]},
                      {"id": "rvc-ffprobe.exe", "path": "tools/RVC/RVC-beta-v2-0618/ffprobe.exe",
                       "files": ["tools/RVC/RVC-beta-v2-0618/ffprobe.exe"]},
                  ]}
        models = {"schema_version": 1, "assets": []}
        manifest_dir = root / "runtime-manifest"
        manifest_dir.mkdir(parents=True)
        documents = {"inventory": inventory, "packages": packages, "vendor": vendor, "models": models}
        paths = {"inventory": "runtime-manifest/inventory.json", "packages": "runtime-manifest/packages.lock.json",
                 "vendor": "runtime-manifest/vendor.json", "models": "runtime-manifest/models.json"}
        artifacts = {name: {"path": paths[name], "sha256": _digest(doc)} for name, doc in documents.items()}
        artifacts["inventory"]["file_count"] = len(records)
        for name, doc in documents.items():
            filename = "packages.lock.json" if name == "packages" else name + ".json"
            (manifest_dir / filename).write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        summary = {
            "schema_version": 1, "snapshot_kind": "current-installed-local-bundle",
            "generated_at_utc": "2026-10-05T00:00:00Z",
            "distribution_status": "UNKNOWN; public distribution is not cleared by this snapshot",
            "source_status": "UNKNOWN", "license_status": "UNKNOWN",
            "python": {key: runtime_python[key] for key in ("implementation", "version", "version_full", "machine", "bits", "executable")},
            "package_count": 1, "artifacts": artifacts,
        }
        summary["snapshot_sha256"] = _digest({key: value for key, value in summary.items() if key != "generated_at_utc"})
        (manifest_dir / "runtime.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        (root / "tools" / "RVC" / "RVC-beta-v2-0618" / "runtime").mkdir(parents=True)
        return summary, inventory, packages

    def _resign_packages(self, root, packages):
        manifest = Path(root) / "runtime-manifest"
        (manifest / "packages.lock.json").write_text(
            json.dumps(packages, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        documents = {
            "inventory": json.loads((manifest / "inventory.json").read_text(encoding="utf-8")),
            "packages": packages,
            "vendor": json.loads((manifest / "vendor.json").read_text(encoding="utf-8")),
            "models": json.loads((manifest / "models.json").read_text(encoding="utf-8")),
        }
        summary = json.loads((manifest / "runtime.json").read_text(encoding="utf-8"))
        for name, document in documents.items():
            summary["artifacts"][name]["sha256"] = _digest(document)
        summary["snapshot_sha256"] = _digest({key: value for key, value in summary.items()
                                               if key not in ("generated_at_utc", "snapshot_sha256")})
        (manifest / "runtime.json").write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    def _ffmpeg_runner(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 0,
                                            stdout=("ffmpeg version " + verify.EXPECTED_FFMPEG_BUILD + " fixture\n").encode(),
                                            stderr=b"")

    def test_new_component_bound_snapshot_accepts_and_rejects_catalog_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._root(directory)
            summary,_,_=self._fixture(root)
            catalog=json.loads((ROOT/'runtime-manifest/components.json').read_text(encoding='utf-8'))
            path=root/'runtime-manifest/components.json'
            path.write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
            summary['artifacts']['components']={'path':'runtime-manifest/components.json','sha256':_digest(catalog)}
            summary['snapshot_sha256']=_digest({key:value for key,value in summary.items() if key not in ('generated_at_utc','snapshot_sha256')})
            (root/'runtime-manifest/runtime.json').write_text(json.dumps(summary),encoding='utf-8')
            self.assertEqual(self._verify(root)['status'],'PASS_WITH_WARNINGS')
            catalog['components'][0]['version']='tampered-version'
            path.write_text(json.dumps(catalog),encoding='utf-8')
            result=self._verify(root)
            self.assertEqual(result['status'],'FAIL')
            self.assertIn('digest does not match: components',result['error'])

    def test_component_descriptor_requires_real_valid_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._root(directory);summary,_,_=self._fixture(root)
            summary['artifacts']['components']={'path':'runtime-manifest/components.json','sha256':_digest({'schema_version':True,'components':[]})}
            summary['snapshot_sha256']=_digest({key:value for key,value in summary.items() if key not in ('generated_at_utc','snapshot_sha256')})
            (root/'runtime-manifest/runtime.json').write_text(json.dumps(summary),encoding='utf-8')
            self.assertEqual(self._verify(root)['status'],'FAIL')
            (root/'runtime-manifest/components.json').write_text(json.dumps({'schema_version':True,'components':[]}),encoding='utf-8')
            self.assertEqual(self._verify(root)['status'],'FAIL')

    def _verify(self, root, data_root=None, **kwargs):
        return verify.verify_runtime(root=root, data_root=data_root or Path(root).parent / "user-data",
                                     python_override=sys.executable, ffmpeg_runner=self._ffmpeg_runner, **kwargs)

    def test_valid_manifest_python_imports_ffmpeg_and_absent_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            data = Path(directory) / "user-data"
            data.mkdir()
            result = self._verify(root, data_root=data)
            self.assertEqual(result["status"], "PASS_WITH_WARNINGS")
            self.assertEqual(result["checks"]["inventory"]["file_count"], 10 + len(verify.REQUIRED_MODULES))
            self.assertEqual(result["checks"]["python"]["status"], "PASS")
            self.assertEqual(result["checks"]["ffmpeg"]["ffmpeg.exe"]["status"], "PASS")
            self.assertEqual(result["checks"]["database"]["status"], "N/A")
            self.assertFalse(list(data.glob(".amp-runtime-verify-*.tmp")))

    def test_missing_and_tampered_runtime_files_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            (root / "runtime" / "bin" / "ffmpeg.exe").write_bytes(b"tampered")
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("hash differs", result["error"])

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            (root / "core" / "fixture.py").unlink()
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("missing", result["error"])

    def test_vendor_cwd_ffmpeg_must_match_embedded_runtime_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            manifest = root / "runtime-manifest"
            inventory = json.loads((manifest / "inventory.json").read_text(encoding="utf-8"))
            vendor_path = "tools/RVC/RVC-beta-v2-0618/ffmpeg.exe"
            vendor_record = next(item for item in inventory["files"] if item["path"] == vendor_path)
            vendor_record["sha256"] = hashlib.sha256(b"different ffmpeg build").hexdigest()
            vendor_record["size"] = len(b"different ffmpeg build")
            (manifest / "inventory.json").write_text(
                json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            summary = json.loads((manifest / "runtime.json").read_text(encoding="utf-8"))
            summary["artifacts"]["inventory"]["sha256"] = _digest(inventory)
            summary["snapshot_sha256"] = _digest({key: value for key, value in summary.items()
                                                  if key not in ("generated_at_utc", "snapshot_sha256")})
            (manifest / "runtime.json").write_text(
                json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("vendor CWD tool does not match runtime/bin", result["error"])

    def test_unmanifested_file_inside_runtime_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            self._write(root, "runtime/addons/untracked.py", b"unexpected\n")
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("file set differs", result["error"])

    def test_existing_bytecode_cache_is_rejected_even_when_manifest_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            cache = root / "runtime" / "addons" / "__pycache__"
            cache.mkdir()
            (cache / "untracked.cpython-39.pyc").write_bytes(b"unmanifested executable cache")
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("bytecode cache", result["error"])

    def test_fast_inventory_check_rejects_bytecode_cache_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            cache = root / 'core' / '__pycache__'
            cache.mkdir(parents=True)
            preserved = cache / 'user.pyc'
            preserved.write_bytes(b'unchanged')
            with self.assertRaisesRegex(verify.RuntimeVerificationError,'bytecode cache'):
                verify._collect_actual_paths(root, ['core'], fast=True)
            self.assertEqual(preserved.read_bytes(), b'unchanged')

    def test_refrozen_manifest_cannot_omit_a_required_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            _, _, packages = self._fixture(root)
            del packages["module_resolution"]["torchcrepe"]
            self._resign_packages(root, packages)
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("omits required imports", result["error"])

    def test_refrozen_manifest_with_required_import_not_found_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            _, _, packages = self._fixture(root)
            packages["module_resolution"]["torchcrepe"]["status"] = "NOT_FOUND"
            packages["module_resolution"]["torchcrepe"]["origin"] = None
            self._resign_packages(root, packages)
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("required runtime imports are unavailable", result["error"])

    def test_release_sentinel_without_valid_plain_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            (root / PORTABLE_RELEASE_MARKER).unlink()
            (root / RELEASE_MANIFEST).write_text("local release fixture", encoding="utf-8")
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("plain PORTABLE_RELEASE file", result["error"])

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            marker = root / PORTABLE_RELEASE_MARKER
            marker.unlink()
            marker.mkdir()
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("plain PORTABLE_RELEASE file", result["error"])

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            (root / PORTABLE_RELEASE_MARKER).write_text("damaged marker\n", encoding="ascii")
            result = self._verify(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("marker content is invalid", result["error"])

    def test_actual_required_import_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            _, _, packages = self._fixture(root)
            (root / "runtime" / "addons" / "torchcrepe.py").write_text(
                "raise ImportError('fixture dependency missing')\n", encoding="utf-8")
            with self.assertRaisesRegex(verify.RuntimeVerificationError, "required module import failed"):
                verify._probe_python(root, packages, 30, root.parent / "user-data", python_override=sys.executable)

    def test_existing_database_is_read_only_and_schema_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            data = Path(directory) / "user-data"
            database = data / "output" / "database" / "app.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            for table, columns in verify.REQUIRED_DB_COLUMNS.items():
                definitions = ", ".join(name + " TEXT" for name in sorted(columns))
                connection.execute("CREATE TABLE " + table + " (" + definitions + ")")
            connection.commit()
            connection.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            result = self._verify(root, data_root=data)
            after = hashlib.sha256(database.read_bytes()).hexdigest()
            self.assertIn(result["status"], ("PASS_WITH_WARNINGS", "PASS"))
            self.assertEqual(result["checks"]["database"]["status"], "PASS")
            self.assertEqual(before, after)

    def test_bad_database_or_unwritable_data_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            data = Path(directory) / "user-data"
            database = data / "output" / "database" / "app.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
            connection.close()
            result = self._verify(root, data_root=data)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("missing required tables", result["error"])

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            data_file = root / "not-a-directory"
            data_file.write_text("file", encoding="utf-8")
            result = self._verify(root, data_root=data_file)
            self.assertEqual(result["status"], "FAIL")

    def test_data_root_must_be_absolute_and_outside_program_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            relative = self._verify(root, data_root=Path("relative-data"))
            self.assertEqual(relative["status"], "FAIL")
            self.assertIn("absolute", relative["error"])
            nested = self._verify(root, data_root=root / "runtime" / "data")
            self.assertEqual(nested["status"], "FAIL")
            self.assertIn("outside", nested["error"])

    def test_write_probe_permission_error_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="amp readonly probe ") as directory:
            with mock.patch.object(verify.tempfile, "mkstemp", side_effect=PermissionError("fixture is read-only")):
                with self.assertRaisesRegex(verify.RuntimeVerificationError, "not writable"):
                    verify._probe_data_writable(Path(directory))

    def test_probe_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._fixture(root)
            with mock.patch.object(verify.subprocess, "run", side_effect=subprocess.TimeoutExpired("python", 1)):
                result = verify.verify_runtime(root=root, data_root=root.parent / "user-data", timeout=5,
                                               python_override=sys.executable, ffmpeg_runner=self._ffmpeg_runner)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("timed out", result["error"])

    def test_cpu_only_probe_is_warning_and_not_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            _, _, packages = self._fixture(root)
            expected = [{key: package.get(key) for key in ("normalized_name", "version", "site_root", "search_order")}
                        for package in packages["distributions"]]
            mock_probe = {
                "version": platform.python_version(), "bits": struct.calcsize("P") * 8,
                "packages": expected,
                "modules": {"demo_pkg": {"status": "FOUND", "origin": "runtime/addons/demo_pkg/__init__.py"},
                            **{name: {"status": "FOUND", "origin": "runtime/addons/" + name + ".py"}
                               for name in verify.REQUIRED_MODULES}},
                "cuda": {"available": False, "error": "CUDA device unavailable"},
            }
            with mock.patch.object(verify, "_probe_python", return_value=mock_probe):
                result = self._verify(root)
            self.assertIn(result["status"], ("PASS_WITH_WARNINGS", "PASS"))
            self.assertEqual(result["checks"]["cuda"]["status"], "WARN")

    def test_cli_branch_short_circuits_tk_config_and_storage_imports(self):
        entry = ROOT / "AI_Music_Processor.pyw"
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in ("tkinter", "core.config", "core.pipeline", "core.storage"):
                raise AssertionError("runtime verify imported application module: " + name)
            return original_import(name, *args, **kwargs)

        with mock.patch.object(verify, "main", return_value=17) as main, \
                mock.patch.object(sys, "argv", [str(entry), "--verify-runtime", "--timeout-seconds", "30"]), \
                mock.patch("builtins.__import__", side_effect=guarded_import):
            with self.assertRaises(SystemExit) as raised:
                with contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(str(entry), run_name="__main__")
        self.assertEqual(raised.exception.code, 17)
        main.assert_called_once_with(["--timeout-seconds", "30"])

    def test_business_cli_release_failure_precedes_pipeline_and_config_imports(self):
        from core import paths
        entry = ROOT / "process_music.py"
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in ("core.config", "core.pipeline", "core.processes", "core.storage"):
                raise AssertionError("release CLI imported business module before verification: " + name)
            return original_import(name, *args, **kwargs)

        with mock.patch.object(paths, "is_release_mode", return_value=True), \
                mock.patch.object(verify, "verify_runtime", return_value={"status": "FAIL", "error": "fixture tamper"}) as gate, \
                mock.patch.object(sys, "argv", [str(entry), "--diagnose"]), \
                mock.patch("builtins.__import__", side_effect=guarded_import):
            with self.assertRaises(SystemExit) as raised:
                with contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(str(entry), run_name="__main__")
        self.assertEqual(raised.exception.code, 1)
        gate.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "GUI release entry is Windows-only")
    def test_smoke_gui_release_failure_precedes_tk_config_and_pipeline_imports(self):
        import ctypes
        entry = ROOT / "AI_Music_Processor.pyw"
        marker = str(ROOT / "PORTABLE_RELEASE")
        original_isfile = os.path.isfile
        original_import = builtins.__import__
        previous_bytecode_setting = sys.dont_write_bytecode
        inserted_root = str(ROOT)

        def marked_release(path):
            return True if os.path.normcase(os.fspath(path)) == os.path.normcase(marker) else original_isfile(path)

        def guarded_import(name, *args, **kwargs):
            if name in ("tkinter", "core.config", "core.pipeline", "core.storage"):
                raise AssertionError("GUI release imported application module before verification: " + name)
            return original_import(name, *args, **kwargs)

        try:
            with tempfile.TemporaryDirectory(prefix="amp gui data ") as data_root, \
                    mock.patch.dict(os.environ, {"AI_MUSIC_PROCESSOR_DATA_ROOT": data_root}), \
                    mock.patch.object(os.path, "lexists", side_effect=marked_release), \
                    mock.patch.object(verify, "verify_runtime", return_value={"status": "FAIL", "error": "tampered"}) as gate, \
                    mock.patch.object(sys, "argv", [str(entry), '--smoke-test']), \
                    mock.patch("ctypes.WinDLL") as win_dll, \
                    mock.patch("builtins.__import__", side_effect=guarded_import):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(entry), run_name="__main__")
            self.assertEqual(raised.exception.code, 1)
            gate.assert_called_once()
            win_dll.assert_not_called()
        finally:
            sys.dont_write_bytecode = previous_bytecode_setting
            if sys.path and sys.path[0] == inserted_root:
                sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()
