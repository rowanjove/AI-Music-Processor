import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseCliControlFlowTests(unittest.TestCase):
    def test_real_release_marker_continues_processing_only_after_healthy_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / 'process_music.py'
            entry.write_bytes((ROOT / 'process_music.py').read_bytes())
            (root / 'PORTABLE_RELEASE').write_text('AI Music Processor Portable Release v1', encoding='ascii')
            runner = root / 'runner.py'
            runner.write_text('''
import sys, types, runpy, json
sys.path.insert(0, sys.argv[1])
from core.paths import is_release_mode
assert is_release_mode(sys.argv[2]) is True
from core import runtime_verify
gate_status = sys.argv[3]
runtime_verify.verify_runtime = lambda **kwargs: {'status': gate_status}
pipeline = types.ModuleType('core.pipeline')
class Storage:
    def close(self): pass
class Pipeline:
    def __init__(self): self.storage = Storage()
    def process_file(self, path, **kwargs):
        print('ACTUAL_PROCESS_CALLED:' + path)
        return {'status': 'DONE', 'job_id': 'fixture', 'output_paths': {}}
pipeline.AudioPipeline = Pipeline
sys.modules['core.pipeline'] = pipeline
sys.argv = [sys.argv[2] + '/process_music.py'] + sys.argv[4:]
runpy.run_path(sys.argv[0], run_name='__main__')
''', encoding='utf-8')
            env = dict(os.environ, AI_MUSIC_PROCESSOR_DATA_ROOT=str(root / 'data'),
                       PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
            for status, arguments, exit_code, processed in (
                    ('PASS', ['source.wav', '--formats', 'wav'], 0, True),
                    ('PASS_WITH_WARNINGS', ['source.wav'], 0, True),
                    ('FAIL', ['source.wav'], 1, False),
                    ('PASS', ['--verify-runtime'], 0, False)):
                with self.subTest(status=status, arguments=arguments):
                    result = subprocess.run([sys.executable, '-B', str(runner), str(ROOT), str(root),
                                             status, *arguments], env=env, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, exit_code, result.stderr.decode('utf-8', 'replace'))
                    self.assertEqual(b'ACTUAL_PROCESS_CALLED:' in result.stdout, processed)
                    if processed:
                        self.assertIn(b'"DONE"', result.stdout)
                    else:
                        self.assertIn(('"%s"' % status).encode('ascii'), result.stdout)


if __name__ == '__main__':
    unittest.main()
