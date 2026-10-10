import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.component_platform import build_registry, catalog_path, load_catalog
from core.install_platform import (
    bootstrap_decision,
    build_install_plan,
    choose_install_location,
    component_center_rows,
    import_user_model,
    incomplete_install,
    install_offline_package,
    measure_bytes,
    read_first_run,
    repair_managed,
    stage_install,
    transfer_local,
    update_with_rollback,
    write_first_run,
)
from core.storage import sha256_file
from core.storage_policy import cleanup_forbidden


class InstallPlatformTests(unittest.TestCase):
    def test_each_update_preserves_the_immediately_previous_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live, source, backups = root / 'live.bin', root / 'new.bin', root / 'backups'
            live.write_bytes(b'v1')
            source.write_bytes(b'v2')
            first = update_with_rollback(live, source, sha256_file(source), backups)
            source.write_bytes(b'v3')
            second = update_with_rollback(live, source, sha256_file(source), backups)
            self.assertFalse(first['rolled_back'])
            self.assertFalse(second['rolled_back'])
            self.assertNotEqual(first['previous'], second['previous'])
            self.assertEqual(Path(first['previous']).read_bytes(), b'v1')
            self.assertEqual(Path(second['previous']).read_bytes(), b'v2')
            self.assertEqual(live.read_bytes(), b'v3')

    def test_invalid_model_import_never_replaces_existing_user_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'untrusted.bin'
            source.write_bytes(b'not a verified model/index')
            user = root / 'user/imported'
            user.mkdir(parents=True)
            for extension in ('.pth', '.index'):
                target = user / ('voice' + extension)
                target.write_bytes(b'preserve old user asset')
                with self.assertRaises(ValueError):
                    import_user_model(source, target.name, root / 'user', root / 'managed', [])
                self.assertEqual(target.read_bytes(), b'preserve old user asset')
                self.assertEqual(list(user.glob('*.tmp')), [])

    def test_plan_sizes_marker_and_location_follow_the_catalog(self):
        catalog = load_catalog(catalog_path(ROOT))
        polish = build_install_plan(catalog, ["polish"])
        kept = build_install_plan(catalog, ["polish", "transcribe"])
        self.assertNotIn("engine.rvc", polish["component_ids"])
        self.assertNotIn("engine.rvc", kept["component_ids"])
        self.assertIn("runtime.torch", kept["component_ids"])
        self.assertEqual(polish["urls"], [])
        self.assertTrue(all(row["id"] in {item["id"] for item in catalog["components"]} for row in polish["installable"]))
        self.assertNotIn("engine.f0.rmvpe", [row["id"] for row in polish["installable"]])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.bin").write_bytes(b"abc")
            (root / "b.bin").write_bytes(b"12345")
            measured = measure_bytes(folder)
            self.assertEqual(measured["bytes"], 8)
            self.assertEqual(measured["unit"], "bytes")
            self.assertNotIn("gigabytes", measured)
            marker = root / "state" / "first-run.json"
            marker.parent.mkdir()
            marker.write_text("{", encoding="utf-8")
            self.assertFalse(read_first_run(str(marker))["complete"])
            self.assertTrue(write_first_run(str(marker))["complete"])
            self.assertNotIn("database", str(marker).replace("/", os.sep).lower())
            program = root / "program"
            program.mkdir()
            inside = {"AI_MUSIC_PROCESSOR_DATA_ROOT": str(program)}
            with self.assertRaises(ValueError):
                choose_install_location(str(program), environ=inside, release_mode=True)
            outside = root / "user-data"
            outside.mkdir()
            chosen = choose_install_location(
                str(program), environ={"AI_MUSIC_PROCESSOR_DATA_ROOT": str(outside)}, release_mode=True,
            )
            dev = choose_install_location(str(program), release_mode=False)
        self.assertNotEqual(os.path.normcase(chosen["data_root"]), os.path.normcase(chosen["program_root"]))
        self.assertEqual(os.path.normcase(dev["data_root"]), os.path.normcase(dev["program_root"]))

    def test_local_transfer_resume_checksum_staging_repair_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "package.bin"
            source.write_bytes(b"abcdefghij")
            digest = sha256_file(str(source))
            live = root / "runtime.bin"
            live.write_bytes(b"live")
            partial = transfer_local(str(source), str(root / "fresh.bin"), digest, interrupt_after=4)
            self.assertFalse(partial["complete"])
            self.assertTrue(partial["destination_unchanged"])
            finished = transfer_local(str(root / "package.bin"), str(root / "fresh.bin"), digest)
            self.assertTrue(finished["complete"])
            self.assertEqual(Path(finished["destination"]).read_bytes(), b"abcdefghij")
            with self.assertRaises(ValueError):
                transfer_local(str(source), str(live), "0" * 64)
            self.assertEqual(live.read_bytes(), b"live")
            with self.assertRaises(OSError):
                transfer_local(str(source), str(root / "full.bin"), digest, free_bytes=1, required_bytes=10)
            self.assertFalse((root / "full.bin").exists())
            staged = stage_install(str(source), str(live), str(root / "stage"), digest, commit=False)
            self.assertFalse(staged["committed"])
            self.assertTrue(staged["live_unchanged"])
            self.assertTrue(incomplete_install(str(root / "stage")))
            self.assertEqual(live.read_bytes(), b"live")
            package = root / "pkg"
            live_root = root / "live"
            (package / "runtime").mkdir(parents=True)
            repair_source = package / 'runtime' / 'small.txt'
            repair_source.write_text("fixed", encoding="utf-8")
            (live_root / "runtime").mkdir(parents=True)
            (live_root / 'runtime-manifest').mkdir()
            (live_root / 'runtime-manifest/inventory.json').write_text(json.dumps({'files': [
                {'path': 'runtime/small.txt', 'sha256': sha256_file(str(repair_source))}]}), encoding='utf-8')
            user = live_root / "models" / "rvc" / "mine.pth"
            user.parent.mkdir(parents=True)
            user.write_bytes(b"user-voice")
            log = root / "repair.log"
            repair_managed(str(package), str(live_root), ["runtime/small.txt"], str(log))
            self.assertEqual((live_root / "runtime" / "small.txt").read_text(encoding="utf-8"), "fixed")
            self.assertEqual(user.read_bytes(), b"user-voice")
            self.assertIn("runtime/small.txt", log.read_text(encoding="utf-8"))
            bad = root / "bad.bin"
            bad.write_bytes(b"nope")
            busy = update_with_rollback(str(live), str(source), digest, str(root / "busy"), in_use=True)
            self.assertTrue(busy["live_unchanged"])
            self.assertEqual(live.read_bytes(), b"live")
            rolled = update_with_rollback(str(live), str(bad), digest, str(root / "backup"))
            self.assertTrue(rolled["live_unchanged"])
            self.assertEqual(live.read_bytes(), b"live")
            offline = root / "offline"
            (offline / "runtime").mkdir(parents=True)
            payload = offline / "runtime" / "piece.bin"
            payload.write_bytes(b"offline")
            manifest = {
                "files": [{"path": "runtime/piece.bin", "sha256": sha256_file(str(payload))}],
            }
            (offline / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            install_root = root / 'installed'
            (install_root / 'runtime-manifest').mkdir(parents=True)
            (install_root / 'runtime-manifest/inventory.json').write_text(json.dumps(manifest), encoding='utf-8')
            installed = install_offline_package(str(offline), str(install_root))
            self.assertFalse(installed["network"])
            self.assertTrue(Path(installed["installed"][0]).read_bytes() == b"offline")
            managed = root / "weights"
            managed.mkdir()
            managed_file = managed / "zhoujielun.pth"
            managed_file.write_bytes(b"managed")
            before = managed_file.read_bytes()
            from unittest import mock
            # This fixture isolates atomic import ownership/IO. Checkpoint and
            # native-index semantic validation have separate adversarial tests.
            with mock.patch('core.install_platform._validate_user_model') as validator:
                imported = import_user_model(str(source), "custom.pth", str(root / "models" / "rvc"), str(managed), ["zhoujielun.pth"])
                validator.assert_called_once()
            self.assertEqual(imported["ownership"], "user")
            self.assertTrue(str(imported["destination"]).replace("/", os.sep).endswith(os.path.join("models", "rvc", "imported", "custom.pth")))
            self.assertEqual(managed_file.read_bytes(), before)

    def test_component_center_and_bootstrap_do_not_weaken_the_launcher(self):
        rows = component_center_rows(build_registry(ROOT))
        rmvpe = next(row for row in rows if row["id"] == "engine.f0.rmvpe")
        self.assertEqual(rmvpe["label"], "可用")
        self.assertNotEqual(rmvpe["label"], "未安装")
        self.assertTrue(rmvpe["implemented"])
        missing_python = bootstrap_decision(False, True, False)
        self.assertFalse(missing_python["launch_main"])
        blocked = bootstrap_decision(True, True, False)
        self.assertFalse(blocked["launch_main"])
        ready = bootstrap_decision(True, False, None)
        self.assertTrue(ready["launch_main"])
        launcher = (ROOT / "app" / "Launcher.cs").read_text(encoding="utf-8")
        self.assertIn("主程序未启动", launcher)
        self.assertIn('if(fullVerify) return 0', launcher)
        self.assertTrue(cleanup_forbidden(ROOT / "models" / "rvc" / "custom.pth"))
        self.assertTrue(cleanup_forbidden(ROOT / "output" / "song" / "master" / "song.wav"))
        self.assertFalse(cleanup_forbidden(ROOT / "output" / "song" / ".work" / "06_mixed.wav"))
        source = (ROOT / "core" / "install_platform.py").read_text(encoding="utf-8")
        self.assertNotIn("urllib", source)
        self.assertNotIn("import core.pipeline", source)

    def test_offline_package_cannot_write_user_or_unknown_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / 'live'
            (live / 'runtime-manifest').mkdir(parents=True)
            (live / 'runtime-manifest/inventory.json').write_text(json.dumps({'files': []}), encoding='utf-8')
            for relative in ('config/app.json', 'output/database/app.db', 'models/rvc/user.pth', 'core/unknown.py'):
                with self.subTest(relative=relative):
                    package = root / 'package'
                    payload = package / relative
                    payload.parent.mkdir(parents=True, exist_ok=True)
                    payload.write_bytes(b'hostile')
                    (package / 'manifest.json').write_text(json.dumps({'files': [
                        {'path': relative, 'sha256': sha256_file(str(payload))}]}), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        install_offline_package(str(package), str(live))
                    self.assertFalse((live / relative).exists())

    def test_model_import_rejects_windows_drive_ads_and_device_names(self):
        from core.install_platform import plan_model_import
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('F:escape.pth', 'voice.pth:stream', 'CON.pth', 'LPT1.pth', 'voice.pth.'): 
                with self.subTest(name=name), self.assertRaises(ValueError):
                    plan_model_import(name, root / 'user', root / 'managed', [])

    def test_resume_from_another_local_source_restarts_the_partial_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.bin'
            target = Path(directory) / 'target.bin'
            source.write_bytes(b'correct payload')
            Path(str(target) + '.part').write_bytes(b'wrong')
            result = transfer_local(str(source), str(target), sha256_file(str(source)))
            self.assertTrue(result['complete'])
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_complete_portable_donor_repairs_only_active_hash_pinned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live, donor = root / 'live', root / 'portable'
            (live / 'runtime-manifest').mkdir(parents=True)
            (live / 'core').mkdir()
            (donor / 'core').mkdir(parents=True)
            (donor / 'config').mkdir()
            managed = donor / 'core/component.py'
            managed.write_bytes(b'correct component')
            user = donor / 'config/app.json'
            user.write_bytes(b'never copy user data')
            row = {'path': 'core/component.py', 'sha256': sha256_file(str(managed))}
            (live / 'core/component.py').write_bytes(b'broken')
            (live / 'runtime-manifest/inventory.json').write_text(json.dumps({'files': [row]}), encoding='utf-8')
            (donor / 'release-manifest.json').write_text(json.dumps({'files': [row,
                {'path': 'config/app.json', 'sha256': sha256_file(str(user))}]}), encoding='utf-8')
            result = install_offline_package(str(donor), str(live))
            self.assertEqual(result['status'], 'INSTALLED')
            self.assertEqual((live / 'core/component.py').read_bytes(), managed.read_bytes())
            self.assertFalse((live / 'config/app.json').exists())
            self.assertEqual(install_offline_package(str(donor), str(live))['status'], 'NO_CHANGE')

    def test_target_junction_is_rejected_before_any_copy(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.bin'
            source.write_bytes(b'correct')
            outside = root / 'outside'
            outside.mkdir()
            link = root / 'live'
            try:
                os.symlink(str(outside), str(link), target_is_directory=True)
            except OSError:
                if os.name != 'nt':
                    self.skipTest('symlink is unavailable')
                created = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(outside)],
                                         capture_output=True, timeout=15)
                if created.returncode:
                    self.skipTest('junction creation is unavailable')
            target = root / 'live' / 'tool.bin'
            try:
                with self.assertRaises(ValueError):
                    transfer_local(str(source), str(target), sha256_file(str(source)))
                self.assertFalse((outside / 'tool.bin').exists())
            finally:
                os.rmdir(link)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
