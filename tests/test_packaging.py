import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_portable as build


class PortablePackagingTests(unittest.TestCase):
    def test_utf8_capture_preserves_unicode_json_and_rejects_invalid_stdout(self):
        with tempfile.TemporaryDirectory(prefix="amp utf8 capture ") as directory:
            env = build._windows_only_env(Path(directory) / "user-data")
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
            value = {"path": "合成测试音频 数据根 🎵.wav"}
            command = [sys.executable, "-B", "-c",
                       "import json; print(json.dumps(" + repr(value) + ", ensure_ascii=False))"]
            result = build._run_utf8_capture(command, directory, env, timeout=30)
            self.assertEqual(json.loads(result.stdout), value)

            invalid = [sys.executable, "-B", "-c",
                       "import sys; sys.stdout.buffer.write(bytes([255]))"]
            with self.assertRaises(UnicodeDecodeError):
                build._run_utf8_capture(invalid, directory, env, timeout=30)

    def test_module_loader_bootstraps_source_root_without_cwd_or_pythonpath(self):
        with tempfile.TemporaryDirectory(prefix="amp isolated source ") as source_dir:
            source = Path(source_dir)
            (source / "core").mkdir()
            (source / "scripts").mkdir()
            (source / "core/__init__.py").write_text("", encoding="utf-8")
            (source / "core/paths.py").write_text("TOKEN = 'fixture-root'\n", encoding="utf-8")
            (source / "scripts/probe.py").write_text(
                "from core.paths import TOKEN\nVALUE = TOKEN\n", encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="amp unrelated cwd ") as other_cwd:
                loader = (
                    "import importlib.util, pathlib, sys; "
                    "script=pathlib.Path(sys.argv[1]); "
                    "sys.path.insert(0, str(script.parent)); "
                    "spec=importlib.util.spec_from_file_location('_amp_builder_fixture', script); "
                    "builder=importlib.util.module_from_spec(spec); spec.loader.exec_module(builder); "
                    "loaded=builder._load_script_module(pathlib.Path(sys.argv[2]), "
                    "'scripts/probe.py', '_amp_fixture_probe'); print(loaded.VALUE)"
                )
                result = subprocess.run(
                    [sys.executable, "-B", "-I", "-c", loader,
                     str(ROOT / "scripts/build_portable.py"), str(source)],
                    cwd=other_cwd, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "fixture-root")

    def _write(self, root, relative, data=b"fixture\n"):
        path = Path(root, *Path(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _source_fixture(self, root):
        root = Path(root)
        files = [
            "AI_Music_Processor.pyw", "process_music.py", "VERSION", "README.md", "CHANGELOG.md",
            "THIRD_PARTY_LICENSES.md", "LICENSES/manifest.json", "LICENSES/texts/fixture.txt",
            "scripts/build_component_package.py",
            "scripts/run_portable_acceptance.py", "docs/FRESH_MACHINE_ACCEPTANCE.md",
            "runtime-manifest/components.json",
            "core/config.py", "core/__pycache__/stale.pyc", "runtime/addons/example.py",
            "runtime/bin/ffmpeg.exe", "runtime/bin/ffprobe.exe",
            "tools/RVC/RVC-beta-v2-0618/runtime/python.exe",
            "tools/RVC/RVC-beta-v2-0618/configs/config.json",
            "tools/RVC/RVC-beta-v2-0618/infer_pack/model.py",
            "tools/RVC/RVC-beta-v2-0618/uvr5_pack/utils.py",
            "tools/RVC/RVC-beta-v2-0618/config.py",
            "tools/RVC/RVC-beta-v2-0618/infer_uvr5.py",
            "tools/RVC/RVC-beta-v2-0618/MDXNet.py",
            "tools/RVC/RVC-beta-v2-0618/my_utils.py",
            "tools/RVC/RVC-beta-v2-0618/vc_infer_pipeline.py",
            "tools/RVC/RVC-beta-v2-0618/hubert_base.pt",
            "tools/RVC/RVC-beta-v2-0618/LICENSE",
            "tools/RVC/RVC-beta-v2-0618/使用需遵守的协议-LICENSE.txt",
            "tools/RVC/RVC-beta-v2-0618/weights/voice.pth",
            "tools/RVC/RVC-beta-v2-0618/logs/voice.index",
            "tools/RVC/RVC-beta-v2-0618/uvr5_weights/HP2.pth",
            "tools/RVC/RVC-beta-v2-0618/train/training_script.py",
            "tools/UVR5/models/Demucs_Models/v3_v4_repo/model.py",
            "tools/UVR5/models/Demucs_Models/v3_v4_repo/model.th",
        ]
        for relative in files:
            content = relative.encode("utf-8")
            if relative.endswith(("ffmpeg.exe", "ffprobe.exe")):
                content = b"same accepted embedded executable"
            self._write(root, relative, content)
        self._write(root, "config/app.json", b"user settings")
        self._write(root, "song/user.wav", b"user audio")
        self._write(root, "output/database/app.db", b"user database")
        self._write(root, "tools/Audacity/Audacity.exe", b"optional editor")
        self._write(root, "models/rvc/custom.pth", b"custom user model")
        self._write(root, "tools/RVC/RVC-beta-v2-0618/ffmpeg.exe", b"legacy vendor ffmpeg")
        self._write(root, "tools/RVC/RVC-beta-v2-0618/ffprobe.exe", b"legacy vendor ffprobe")
        (root / "VERSION").write_text("1.0.0-local.fixture\n", encoding="utf-8")
        return root

    def test_allowlist_preserves_inference_assets_but_excludes_user_data_and_editors(self):
        with tempfile.TemporaryDirectory(prefix="amp package source ") as source_dir, \
                tempfile.TemporaryDirectory(prefix="amp package output ") as output_dir:
            source = self._source_fixture(source_dir)
            old_tools = {name: hashlib.sha256((source / build.RVC / name).read_bytes()).hexdigest()
                         for name in ("ffmpeg.exe", "ffprobe.exe")}
            stage = Path(output_dir) / "Portable"
            build.copy_allowlisted_bundle(source, stage)
            self.assertTrue((stage / "runtime-manifest/components.json").is_file())
            for relative in ('THIRD_PARTY_LICENSES.md','LICENSES/manifest.json','LICENSES/texts/fixture.txt','scripts/build_component_package.py'):
                self.assertEqual((stage/relative).read_bytes(),(source/relative).read_bytes())
            self.assertEqual((stage / "runtime-manifest/components.json").read_bytes(),
                             (source / "runtime-manifest/components.json").read_bytes())
            self.assertTrue((stage / build.MARKER).is_file())
            self.assertEqual((stage / build.MARKER).read_text(encoding="ascii"), build.MARKER_CONTENT)
            self.assertEqual(hashlib.sha256((stage / build.RVC / "ffmpeg.exe").read_bytes()).hexdigest(),
                             hashlib.sha256((stage / "runtime/bin/ffmpeg.exe").read_bytes()).hexdigest())
            self.assertEqual(hashlib.sha256((stage / build.RVC / "ffprobe.exe").read_bytes()).hexdigest(),
                             hashlib.sha256((stage / "runtime/bin/ffprobe.exe").read_bytes()).hexdigest())
            self.assertTrue((stage / build.RVC / "weights/voice.pth").is_file())
            self.assertTrue((stage / build.RVC / "LICENSE").is_file())
            self.assertTrue((stage / build.RVC / "使用需遵守的协议-LICENSE.txt").is_file())
            self.assertTrue((stage / build.RVC / "logs/voice.index").is_file())
            self.assertTrue((stage / build.DEMUCS / "model.th").is_file())
            self.assertFalse((stage / build.RVC / "train/training_script.py").exists())
            self.assertEqual(old_tools,
                             {name: hashlib.sha256((source / build.RVC / name).read_bytes()).hexdigest()
                              for name in old_tools},
                             "portable staging must not modify current vendor binaries")
            self.assertFalse((stage / "config").exists())
            self.assertFalse((stage / "song").exists())
            self.assertFalse((stage / "output").exists())
            self.assertFalse((stage / "models/rvc").exists())
            self.assertFalse((stage / "tools/Audacity").exists())
            self.assertFalse(any(path.name == "__pycache__" for path in stage.rglob("*")))

    def test_release_manifest_and_zip_extraction_compare_every_package_file(self):
        with tempfile.TemporaryDirectory(prefix="amp package fixture ") as directory:
            package = Path(directory) / "Portable Unicode 路径"
            package.mkdir()
            (package / build.MARKER).write_text(build.MARKER_CONTENT, encoding="ascii")
            (package / "AI-Music-Processor.exe").write_bytes(b"launcher binary fixture")
            (package / "core.py").write_text("fixture source\n", encoding="utf-8")
            (package / "VERSION").write_text("1.0.0-local.fixture\n", encoding="utf-8")
            (package / 'runtime-manifest').mkdir()
            catalog = {'components': [dict(id=identifier, ownership='managed', kind='engine',
                                          implemented=True, probes=[{'type': 'file', 'path': 'core.py'}])
                                      for identifier in ('fixture.first', 'fixture.second')]}
            (package / 'runtime-manifest/components.json').write_text(json.dumps(catalog), encoding='utf-8')
            runtime_summary = {"snapshot_sha256": "a" * 64, "package_count": 1,
                               "artifacts": {"inventory": {"file_count": 4, "sha256": "b" * 64}}}
            manifest = build.write_release_manifest(package, package, runtime_summary,
                                                    {"path": "csc.exe", "sha256": "c" * 64, "version": "fixture"})
            self.assertEqual(manifest["distribution_status"],
                             "UNKNOWN; public release is blocked pending rights review")
            self.assertNotIn(build.RELEASE_MANIFEST, [item["path"] for item in manifest["files"]])
            footprints = manifest['component_sizes']
            self.assertFalse(footprints['additive'])
            for row in footprints['components'].values():
                self.assertEqual(row['install_bytes'], (package / 'core.py').stat().st_size)
                self.assertEqual(row['shared_install_bytes'], row['install_bytes'])
                self.assertIsNone(row['download_bytes'])
                self.assertIsNone(row['cache_bytes'])
            self.assertEqual(manifest['component_sizes_sha256'], build._json_digest(footprints))
            self.assertEqual(build.verify_release_manifest(package)["status"], "PASS")
            archive = Path(directory) / "portable.zip"
            build.write_zip(package, archive)
            extracted = Path(directory) / "extracted"
            result = build.extract_and_verify(archive, extracted, manifest["file_count"] + 1)
            self.assertEqual(result["status"], "PASS")
            (extracted / "core.py").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                build.verify_release_manifest(extracted)

    def test_zip_rejects_windows_stream_device_and_alias_paths_before_write(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, name in enumerate(('core/model.pth:stream', 'core/CON.pth',
                                          'core/voice.pth.', 'core/voice.pth ',
                                          'core/../outside.py', 'core/evil?.py')):
                with self.subTest(name=name):
                    archive = root / ('bad%d.zip' % index)
                    with zipfile.ZipFile(archive, 'w') as handle:
                        handle.writestr(name, b'hostile')
                    extracted = root / ('extracted%d' % index)
                    with self.assertRaisesRegex(ValueError, 'unsafe package-relative'):
                        build.extract_and_verify(archive, extracted, 1)
                    self.assertEqual(list(extracted.rglob('*')), [])

    def test_cli_result_parser_accepts_pretty_json(self):
        result = {"status": "DONE", "job_id": "fixture",
                  "output_paths": {"master_wav": "a.wav", "reports_dir": "reports"},
                  "qc": {"status": "PASS"}}
        self.assertEqual(build._parse_cli_result(json.dumps(result, indent=2)), result)

    def test_cli_result_parser_ignores_nested_qc_pass_and_requires_trailing_job(self):
        result = {"status": "DONE", "job_id": "fixture",
                  "output_paths": {"master_wav": "a.wav", "reports_dir": "reports"},
                  "qc": {"status": "PASS", "details": {"nested": True}}}
        output = "worker log: qc={\"status\":\"PASS\"}\n" + json.dumps(result, indent=2) + "\n"
        self.assertEqual(build._parse_cli_result(output), result)
        self.assertIsNone(build._parse_cli_result('{"status":"FAILED","qc":{"status":"PASS"}}\n'))
        self.assertIsNone(build._parse_cli_result(json.dumps(result) + "\nfinished\n"))

    def test_cli_job_must_match_independent_project_manifest(self):
        cli = {"status": "DONE", "job_id": "job-fixture", "mode": "polish",
               "output_paths": {"master_wav": "F:/data/master.wav", "reports_dir": "F:/data/reports"},
               "qc": {"status": "PASS"}}
        project = {"status": "DONE", "job_id": "job-fixture", "mode": "polish",
                   "output_paths": {"master_wav": "F:/data/master.wav", "reports_dir": "F:/data/reports"},
                   "qc": {"status": "PASS"}}
        self.assertTrue(build._validate_cli_project_result(cli, project, "polish"))
        mismatched = dict(project, job_id="different-job")
        self.assertFalse(build._validate_cli_project_result(cli, mismatched, "polish"))
        mismatched = dict(project, qc={"status": "WARN"})
        self.assertFalse(build._validate_cli_project_result(cli, mismatched, "polish"))

    def test_any_business_failure_fails_acceptance_and_returns_nonzero(self):
        runtime = {"status": "PASS", "checks": {}}
        modes = {
            "voice_conversion": {"status": "FAIL", "error": "fixture inference failure"},
            "transcribe": {"status": "PASS", "qc_status": "PASS"},
        }
        self.assertEqual(build._acceptance_status(runtime, modes, "PASS"), "FAILED")
        self.assertEqual(build._build_exit_code({"status": "FAILED"}), 1)

    def test_warn_only_comes_from_verified_qc_or_runtime_warning(self):
        modes = {"voice_conversion": {"status": "PASS", "qc_status": "PASS"},
                 "transcribe": {"status": "PASS", "qc_status": "WARN"}}
        runtime_pass = {"status": "PASS", "checks": {}}
        runtime_cpu_only = {"status": "PASS_WITH_WARNINGS", "checks": {
            "cuda": {"status": "WARN", "available": False},
            "database": {"status": "N/A"},
        }}
        runtime_data_warn = {"status": "PASS_WITH_WARNINGS", "checks": {
            "data": {"status": "WARN"},
            "database": {"status": "N/A"},
        }}
        runtime_real_first_install = {"status": "PASS_WITH_WARNINGS", "checks": {
            "cuda": {"status": "PASS", "available": True},
            "data": {"status": "PASS"},
            "database": {"status": "N/A"},
            "disk": {"status": "PASS"},
            "ffmpeg": {"ffmpeg.exe": {"status": "PASS"},
                       "ffprobe.exe": {"status": "PASS"}},
            "inventory": {"status": "PASS"},
            "python": {"status": "PASS"},
        }}
        runtime_nested_ffmpeg_failure = {"status": "PASS_WITH_WARNINGS", "checks": {
            **runtime_real_first_install["checks"],
            "ffmpeg": {"ffmpeg.exe": {"status": "PASS"},
                       "ffprobe.exe": {"status": "FAIL"}},
        }}
        runtime_missing_ffprobe = {"status": "PASS_WITH_WARNINGS", "checks": {
            **runtime_real_first_install["checks"],
            "ffmpeg": {"ffmpeg.exe": {"status": "PASS"}},
        }}
        self.assertEqual(build._acceptance_status(runtime_pass, modes, "PASS"), "PASS_WITH_WARNINGS")
        self.assertEqual(build._acceptance_status(runtime_cpu_only, modes, "PASS"),
                         "PASS_WITH_WARNINGS")
        self.assertTrue(build._runtime_warnings_are_expected(runtime_real_first_install))
        self.assertEqual(build._acceptance_status(runtime_real_first_install, modes, "PASS"),
                         "PASS_WITH_WARNINGS")
        self.assertFalse(build._runtime_warnings_are_expected(runtime_nested_ffmpeg_failure))
        self.assertFalse(build._runtime_warnings_are_expected(runtime_missing_ffprobe))
        self.assertEqual(build._acceptance_status(runtime_nested_ffmpeg_failure, modes, "PASS"),
                         "FAILED")
        self.assertEqual(build._acceptance_status(runtime_data_warn, modes, "PASS"), "FAILED")
        self.assertEqual(build._acceptance_status(
            runtime_pass, {"transcribe": {"status": "PASS"}}, "PASS"), "FAILED")

    def test_cli_main_propagates_failed_acceptance(self):
        with mock.patch.object(build, "build", return_value={"status": "FAILED"}), \
                redirect_stdout(StringIO()):
            self.assertEqual(build.main(["--output-root", "dist/fixture"]), 1)

    def test_versioned_layout_keeps_scratch_outside_the_package(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        paths = build.layout_paths(ROOT / "dist", version)
        self.assertEqual(paths["package"].name, "AI-Music-Processor-v" + version + "-win-x64")
        self.assertEqual(paths["zip"].name, paths["package"].name + "-portable.zip")
        self.assertEqual(paths["zip"].parent, paths["package"].parent)
        self.assertEqual(paths["scratch"].parent, paths["package"].parent)
        self.assertNotIn(paths["package"], paths["scratch"].parents)
        self.assertEqual(paths["sums"].name, "SHA256SUMS.txt")
        self.assertEqual(paths["report"].name, "BUILD_REPORT.md")
        isolated = build.layout_paths(ROOT / "dist", version, "audit-safe-copy")
        self.assertEqual(isolated['package'].name, 'audit-safe-copy')
        self.assertEqual(isolated['sums'].name, 'audit-safe-copy-SHA256SUMS.txt')
        with self.assertRaises(ValueError):
            build.layout_paths(ROOT / 'dist', version, '../unsafe')

    def test_catalog_classification_matches_the_allowlist_without_urls(self):
        raw = (ROOT / "runtime-manifest" / "components.json").read_text(encoding="utf-8")
        self.assertNotIn("http://", raw)
        self.assertNotIn("https://", raw)
        classified = build.classify_catalog(json.loads(raw))
        included = {row["id"] for row in classified["included"]}
        excluded = {row["id"] for row in classified["excluded"]}
        self.assertIn("engine.separator.hp2", included)
        self.assertIn("runtime.torch", included)
        self.assertIn("mode.polish", included)
        components = {item['id']: item for item in json.loads(raw)['components']}
        for identifier in ('engine.f0.rmvpe', 'engine.separator.kim_vocal_2', 'engine.separator.inst_hq_3'):
            self.assertIn(identifier, included if components[identifier]['implemented'] else excluded)
        self.assertIn("model.rvc.user_scan", excluded)
        rendered = json.dumps(classified, ensure_ascii=False)
        self.assertNotIn("http://", rendered)
        self.assertNotIn("https://", rendered)
        facts = build.compatibility_facts(ROOT)
        self.assertEqual(facts["schema_version"], 1)
        self.assertEqual(facts["stage_contract_version"], 2)
        self.assertEqual(facts["public_distribution_status"], "UNKNOWN_BLOCKED")

    def test_completed_test_logs_and_optional_models_are_honest(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'discover.log'
            log.write_text('Ran 17 tests in 1.0s\n\nOK\n', encoding='utf-8')
            self.assertEqual(build.read_discover_log(log)['status'], 'PASS')
            log.write_text('Ran 17 tests in 1.0s\n\nOK\nEXIT:1\n', encoding='utf-8')
            self.assertEqual(build.read_discover_log(log)['status'], 'FAIL')
            with self.assertRaises(RuntimeError):
                build.build(ROOT / 'dist' / 'refused-test-failure', ROOT, discover_log=log)
            for relative in ('tools/UVR5/models/MDX_Net_Models/Kim_Vocal_2.onnx',
                             'tools/RVC/RVC-beta-v2-0618/rmvpe.pt'):
                self.assertTrue(build.allowlisted_relative(relative))

    def test_checksums_and_report_record_the_three_delivery_paths(self):
        with tempfile.TemporaryDirectory(prefix="amp delivery note ") as folder:
            root = Path(folder)
            zip_path = root / "AI-Music-Processor-vtest-win-x64-portable.zip"
            zip_path.write_bytes(b"zip-bytes")
            sums = root / "SHA256SUMS.txt"
            digest = build.write_checksums(sums, zip_path)
            self.assertEqual(digest, hashlib.sha256(b"zip-bytes").hexdigest())
            self.assertEqual(sums.read_text(encoding="utf-8").split()[0], digest)
            self.assertIn(zip_path.name, sums.read_text(encoding="utf-8"))
            report = root / "BUILD_REPORT.md"
            exe = root / "AI-Music-Processor.exe"
            payload = {
                "version": "1.0.0-local.20261005",
                "built_at_utc": "2026-10-07T00:00:00Z",
                "components": {
                    "included": [{"id": "app.core", "version": "1.0.0-local.20261005", "required": True}],
                    "excluded": [{"id": "engine.f0.rmvpe", "reason": "未实现"}],
                },
                "discover": {"status": "PASS", "ran": "Ran 1 test", "exit": "EXIT:0"},
                "verify_status": "PASS",
                "acceptance_status": "BUILT",
                "zip_size": zip_path.stat().st_size,
                "zip_sha256": digest,
                "zip_path": str(zip_path.resolve()),
                "exe_path": str(exe.resolve()),
                "report_path": str(report.resolve()),
                "compatibility": {
                    "schema_version": 1,
                    "stage_contract_version": 2,
                    "public_distribution_status": "UNKNOWN_BLOCKED",
                },
            }
            build.write_build_report(report, payload)
            text = report.read_text(encoding="utf-8")
            self.assertIn(str(zip_path.resolve()), text)
            self.assertIn(str(exe.resolve()), text)
            self.assertIn(str(report.resolve()), text)
            self.assertIn("UNKNOWN_BLOCKED", text)
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)


if __name__ == "__main__":
    unittest.main()
