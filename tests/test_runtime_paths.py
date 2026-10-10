import os
import importlib
import json
import subprocess
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import config, processes
from core.paths import (DATA_ROOT_ENV, PORTABLE_RELEASE_MARKER, is_release_mode,
                        release_data_root, release_numba_cache_dir, application_data_paths)
from core.paths import PORTABLE_RELEASE_CONTENT, RELEASE_MANIFEST


class RuntimePathTests(unittest.TestCase):
    def test_release_data_root_uses_local_app_data_or_explicit_absolute_override(self):
        local = Path('C:/Users/test/AppData/Local')
        self.assertEqual(release_data_root({'LOCALAPPDATA': str(local)}), (local / 'AI-Music-Processor').resolve())
        override = Path('C:/test/Unicode 路径/AI-Music-Processor')
        self.assertEqual(release_data_root({DATA_ROOT_ENV: str(override)}), override.resolve())
        self.assertEqual(release_numba_cache_dir(environ={DATA_ROOT_ENV: str(override)}),
                         override.resolve() / 'cache' / 'numba')
        with self.assertRaises(ValueError):
            release_data_root({DATA_ROOT_ENV: 'relative/data'})

    def test_app_data_paths_keep_dev_layout_and_isolate_release_unicode_root(self):
        with tempfile.TemporaryDirectory(prefix='amp paths ') as directory:
            app_root = Path(directory) / 'AI Music Processor 程序'
            app_root.mkdir()
            (app_root / 'runtime-manifest').mkdir()
            (app_root / 'runtime-manifest' / 'runtime.json').write_text('{}', encoding='utf-8')
            self.assertFalse(is_release_mode(app_root), 'runtime-manifest alone is not a release sentinel')
            dev = application_data_paths(app_root)
            self.assertEqual(dev['config'], app_root / 'config')
            self.assertEqual(dev['output'], app_root / 'output')
            self.assertEqual(dev['temp'], app_root / 'temp')
            self.assertEqual(dev['logs'], app_root / 'logs')
            self.assertFalse((app_root / 'config').exists())

            (app_root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding='ascii')
            data_root = Path(directory) / 'user data 路径'
            release = application_data_paths(app_root, {DATA_ROOT_ENV: str(data_root)})
            self.assertEqual(release['data_root'], data_root.resolve())
            self.assertEqual(release['database'], data_root / 'output' / 'database' / 'app.db')
            self.assertFalse(data_root.exists(), 'path resolution must not create data directories')
            with self.assertRaises(ValueError):
                application_data_paths(app_root, {DATA_ROOT_ENV: str(app_root / 'inside')})
            manifest_root = Path(directory) / 'release-manifest-only'
            manifest_root.mkdir()
            (manifest_root / RELEASE_MANIFEST).write_text('{}', encoding='utf-8')
            self.assertTrue(is_release_mode(manifest_root))
            (manifest_root / RELEASE_MANIFEST).unlink()
            (manifest_root / PORTABLE_RELEASE_MARKER).mkdir()
            self.assertTrue(is_release_mode(manifest_root), 'a damaged marker must remain in release mode')

    def test_config_and_database_defaults_follow_release_root_without_copying_legacy_files(self):
        import core.paths as paths
        import core.storage as storage
        with tempfile.TemporaryDirectory(prefix='amp release config ') as directory:
            app_root = Path(directory) / 'program'
            app_root.mkdir()
            (app_root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding='ascii')
            data_root = Path(directory) / 'user data 路径'
            try:
                with mock.patch.dict(os.environ, {DATA_ROOT_ENV: str(data_root)}):
                    expected = application_data_paths(app_root, {DATA_ROOT_ENV: str(data_root)})
                    self.assertEqual(storage._default_database_path(app_root), str(expected['database']))
                    with mock.patch.object(paths, 'is_release_mode', return_value=True):
                        importlib.reload(config)
                        self.assertEqual(config.RELEASE_MODE, True)
                        self.assertEqual(config.DATA_ROOT, str(data_root.resolve()))
                        self.assertEqual(config.CONFIG_DIR, str(expected['config']))
                        self.assertEqual(config.OUTPUT_DIR, str(expected['output']))
                        self.assertEqual(config.TEMP_DIR, str(expected['temp']))
                        self.assertEqual(config.LOG_DIR, str(expected['logs']))
                        self.assertEqual(config.STARTUP_ERROR_PATH,
                                         str(expected['logs'] / 'startup_error.log'))
                        self.assertFalse((app_root / 'config').exists())
                        self.assertTrue(Path(config.CONFIG_DIR).is_dir())
            finally:
                importlib.reload(config)
                self.assertEqual(config.STARTUP_ERROR_PATH,
                                 os.path.join(config.BASE_DIR, 'startup_error.log'))
        self.assertFalse(data_root.exists(), 'test teardown owns and removes the isolated root')

    def test_gpu_lock_worker_log_and_lmms_config_use_release_data_paths(self):
        from core import processes, storage, transcriber
        from core.paths import application_data_paths
        with tempfile.TemporaryDirectory(prefix='amp release paths ') as directory:
            app_root = Path(directory) / 'program'
            app_root.mkdir()
            (app_root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding='ascii')
            legacy_config = app_root / 'config' / 'lmms.xml'
            legacy_config.parent.mkdir()
            legacy_config.write_bytes(b'legacy LMMS preferences')
            data_root = Path(directory) / 'user data 路径'
            env = {DATA_ROOT_ENV: str(data_root)}
            expected = application_data_paths(app_root, env)
            with mock.patch.dict(os.environ, env):
                self.assertEqual(storage._default_database_path(app_root), str(expected['database']))
                with mock.patch('core.processes._BASE_DIR', str(app_root)):
                    result = subprocess.CompletedProcess(['worker'], 0, stdout=b'ok', stderr=b'')
                    processes._process_log(['worker'], result, 0.25)
                log = expected['logs'] / 'worker.jsonl'
                self.assertTrue(log.is_file())
                self.assertEqual(json.loads(log.read_text(encoding='utf-8'))['exit_code'], 0)
                self.assertEqual(transcriber.lmms_config(base_dir=app_root), str(expected['config'] / 'lmms.xml'))
                self.assertTrue((expected['output'] / 'lmms').is_dir())
                import xml.etree.ElementTree as ET
                lmms = ET.parse(expected['config'] / 'lmms.xml').getroot()
                self.assertEqual(Path(lmms.find('paths').attrib['workingdir'].rstrip('\\/')),
                                 expected['output'] / 'lmms')
                self.assertEqual(legacy_config.read_bytes(), b'legacy LMMS preferences')
                self.assertEqual(storage._default_database_path(app_root), str(expected['database']))
            self.assertFalse((app_root / 'logs').exists())
            self.assertFalse((app_root / 'output').exists())

    @unittest.skipUnless(os.name == 'nt', 'GPU lock implementation is Windows-only')
    def test_gpu_lock_path_moves_to_release_temp_but_stays_in_dev_tree(self):
        from core.gpu import _gpu_lock_path
        with tempfile.TemporaryDirectory(prefix='amp gpu paths ') as directory:
            app_root = Path(directory) / 'program'
            app_root.mkdir()
            self.assertEqual(_gpu_lock_path(app_root), str(app_root / 'temp' / 'gpu.lock'))
            (app_root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding='ascii')
            data_root = Path(directory) / 'user data'
            with mock.patch.dict(os.environ, {DATA_ROOT_ENV: str(data_root)}):
                self.assertEqual(_gpu_lock_path(app_root), str(data_root / 'temp' / 'gpu.lock'))

    def test_development_tool_resolution_keeps_existing_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / 'tools' / 'ffmpeg' / 'ffmpeg.exe'
            local.parent.mkdir(parents=True)
            local.write_bytes(b'developer tool')
            with mock.patch.object(config.shutil, 'which', return_value='system-ffmpeg.exe'):
                self.assertEqual(config.resolve_tool_path('ffmpeg', root, release_mode=False), str(local))

    def test_release_uses_only_embedded_tools_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding='ascii')
            self.assertTrue(is_release_mode(root))
            bin_dir = root / 'runtime' / 'bin'
            bin_dir.mkdir(parents=True)
            ffmpeg = bin_dir / 'ffmpeg.exe'
            ffmpeg.write_bytes(b'embedded ffmpeg')
            self.assertEqual(config.resolve_tool_path('ffmpeg', root), str(ffmpeg))
            with mock.patch.object(config.shutil, 'which', return_value='system-ffmpeg.exe'):
                with self.assertRaises(FileNotFoundError):
                    config.resolve_tool_path('ffprobe', root)
            with self.assertRaises(FileNotFoundError):
                config.resolve_rvc_python(root, release_mode=True)

    def test_worker_environment_pins_tools_without_mutating_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding='ascii')
            bin_dir = root / 'runtime' / 'bin'
            bin_dir.mkdir(parents=True)
            (bin_dir / 'ffmpeg.exe').write_bytes(b'ffmpeg')
            (bin_dir / 'ffprobe.exe').write_bytes(b'ffprobe')
            caller = {'PATH': r'C:\Windows\System32;C:\Windows', 'KEEP_ME': 'yes',
                      DATA_ROOT_ENV: str(root)}
            before = dict(caller)
            child = processes._prepare_subprocess_env(caller, base_dir=root)
            self.assertEqual(caller, before)
            self.assertEqual(child['KEEP_ME'], 'yes')
            self.assertEqual(child['PATH'].split(os.pathsep)[0], str(bin_dir))
            self.assertEqual(child['NUMBA_CACHE_DIR'], str(root / 'cache' / 'numba'))
            self.assertEqual(child['PYTHONDONTWRITEBYTECODE'], '1')
            self.assertIn(r'C:\Windows\System32', child['PATH'])

    def test_release_worker_fails_if_bundled_tools_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PORTABLE_RELEASE_MARKER).write_text(PORTABLE_RELEASE_CONTENT, encoding='ascii')
            with self.assertRaises(FileNotFoundError):
                processes._prepare_subprocess_env({'PATH': r'C:\Windows\System32'}, base_dir=root)

    @unittest.skipUnless(os.name == 'nt', 'bundled runtime is Windows-only')
    def test_numba_jit_cache_is_redirected_to_release_data_root(self):
        root = Path(__file__).resolve().parents[1]
        python = root / 'tools' / 'RVC' / 'RVC-beta-v2-0618' / 'runtime' / 'python.exe'
        strict_roots = [root / 'runtime', root / 'core', root / 'tools' / 'RVC' / 'RVC-beta-v2-0618']
        before = {path.resolve() for base in strict_roots if base.exists()
                  for current, directories, _ in os.walk(base)
                  for path in [Path(current)] if path.name == '__pycache__'}
        with tempfile.TemporaryDirectory(prefix='amp numba data ') as data_directory, \
                tempfile.TemporaryDirectory(prefix='amp numba source ') as source_directory:
            data_root = Path(data_directory)
            source_root = Path(source_directory)
            (source_root / 'jit_fixture.py').write_text(
                'import numba\n@numba.njit(cache=True)\ndef add_one(value): return value + 1\n', encoding='utf-8')
            code = ('import sys; sys.path.insert(0, ' + repr(str(source_root)) + '); '
                    'from jit_fixture import add_one; assert add_one(41) == 42; '
                    'assert add_one(1) == 2; print("jit-cache-ok")')
            env = dict(os.environ)
            env['NUMBA_CACHE_DIR'] = str(release_numba_cache_dir(data_root))
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            result = subprocess.run([str(python), '-B', '-I', '-c', code], cwd=str(root), env=env,
                                    capture_output=True, text=True, timeout=120, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            cache_files = [path for path in (data_root / 'cache' / 'numba').rglob('*') if path.is_file()]
            self.assertTrue(any(path.suffix in ('.nbi', '.nbc') for path in cache_files), cache_files)
        after = {path.resolve() for base in strict_roots if base.exists()
                 for current, directories, _ in os.walk(base)
                 for path in [Path(current)] if path.name == '__pycache__'}
        self.assertEqual(after, before, 'Numba import/JIT must not create program-tree bytecode caches')

    @unittest.skipUnless(os.name == 'nt', 'embedded runtime is Windows-only')
    def test_bundled_ffmpeg_version_and_vendor_load_audio_without_system_ffmpeg_path(self):
        root = Path(__file__).resolve().parents[1]
        python = root / 'tools' / 'RVC' / 'RVC-beta-v2-0618' / 'runtime' / 'python.exe'
        vendor = root / 'tools' / 'RVC' / 'RVC-beta-v2-0618'
        ffmpeg_bin = root / 'runtime' / 'bin'
        self.assertTrue((ffmpeg_bin / 'ffmpeg.exe').is_file())
        self.assertTrue((ffmpeg_bin / 'ffprobe.exe').is_file())
        system_root = os.environ.get('SystemRoot', r'C:\Windows')
        caller_path = os.pathsep.join([os.path.join(system_root, 'System32'), system_root])
        env = dict(os.environ)
        env['PATH'] = caller_path

        with tempfile.TemporaryDirectory(prefix='amp ffmpeg smoke ') as directory:
            source = Path(directory) / 'synthetic.wav'
            with wave.open(str(source), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                frames = b''.join(struct.pack('<h', int(8000 * ((i % 80) - 40) / 40)) for i in range(16000 // 2))
                audio.writeframes(frames)

            with mock.patch.object(processes, 'is_release_mode', return_value=True), \
                    mock.patch.object(processes, '_process_log', return_value=None):
                version = processes.Control().run(['ffmpeg', '-version'], timeout=30, env=env)
                self.assertEqual(version.returncode, 0)
                self.assertIn(b'2024-05-20-git-127ded5078-full_build-www.gyan.dev', version.stdout)
                code = (
                    'import sys; '
                    'sys.path.insert(0, ' + repr(str(vendor)) + '); '
                    'from my_utils import load_audio; '
                    'audio = load_audio(' + repr(str(source)) + ', 16000); '
                    'assert audio.size > 0; print(audio.size)'
                )
                decoded = processes.Control().run([str(python), '-c', code], timeout=60, env=env)
                self.assertEqual(decoded.returncode, 0, decoded.stderr.decode('utf-8', errors='replace'))
                self.assertGreater(int(decoded.stdout.strip().splitlines()[-1]), 0)
            self.assertEqual(env['PATH'], caller_path)


if __name__ == '__main__':
    unittest.main()
