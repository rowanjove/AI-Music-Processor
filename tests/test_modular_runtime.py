import json,tempfile,unittest,sys
import hashlib
from unittest.mock import Mock,patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.runtime_verify import verify_runtime
from core.runtime_startup import RuntimeGate,DeferredPipeline


class ModularRuntimeTests(unittest.TestCase):
    def test_missing_bootstrap_manifest_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'LIGHTWEIGHT_RELEASE').write_text('AI Music Processor Lightweight Release v1')
            result=verify_runtime(root=root,data_root=root.parent/'modular-user-data')
            self.assertEqual(result['status'],'FAIL')

    def test_full_bundle_cannot_downgrade_to_modular_after_manifest_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'LIGHTWEIGHT_RELEASE').write_text('AI Music Processor Lightweight Release v1')
            (root/'release-manifest.json').write_text('{}')
            result=verify_runtime(root=root,data_root=root.parent/'modular-user-data')
            self.assertEqual(result['status'],'FAIL');self.assertIn('cannot fall back',result['error'])

    def test_modular_ready_never_authorizes_voice_or_transcription(self):
        gate=RuntimeGate();gate.finish({'status':'PASS','allowed_modes':['polish']})
        pipe=DeferredPipeline(gate,None,None,None)
        for mode in ('voice_conversion','transcribe'):
            with self.assertRaises(RuntimeError):pipe.process_file('input',mode=mode)

    def test_resume_uses_stored_mode_before_backend_import(self):
        gate=RuntimeGate();gate.finish({'status':'PASS','allowed_modes':['polish']})
        store=Mock();pipe=DeferredPipeline(gate,store,None,None)
        for mode in ('voice_conversion','transcribe'):
            store.get_job.return_value={'mode':mode}
            with self.assertRaises(RuntimeError):pipe.process_file('input',resume_job_id='old')
        store.get_job.return_value={'mode':'polish'}
        with self.assertRaises(RuntimeError):pipe.process_file('input',mode='transcribe',resume_job_id='old')
        store.get_job.return_value=None
        with self.assertRaises(RuntimeError):pipe.process_file('input',resume_job_id='missing')
        self.assertIsNone(pipe._backend)
        store.get_job.return_value={'mode':'polish'}
        pipe._backend=Mock()
        pipe.process_file('input',resume_job_id='good')
        pipe._backend.process_file.assert_called_once()

    def test_unmanifested_pth_and_zip_fail_before_execution(self):
        for extra in ('injected.pth','injected.zip'):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);runtime='tools/RVC/RVC-beta-v2-0618/runtime'
                (root/'LIGHTWEIGHT_RELEASE').write_text('AI Music Processor Lightweight Release v1')
                listed=['AI_Music_Processor.pyw','core/pipeline.py','core/modular_runtime.py',runtime+'/python.exe','runtime/bin/ffmpeg.exe','runtime/bin/ffprobe.exe']
                files=[]
                for name in listed:
                    target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'sealed')
                    files.append({'path':name,'size':6,'sha256':hashlib.sha256(b'sealed').hexdigest()})
                (root/runtime/extra).write_bytes(b'not sealed')
                manifest={'schema_version':1,'profile':'modular-polish-v1','allowed_modes':['polish'],'files':files,'python':{'executable':runtime+'/python.exe','version':'3.9.13'},'tools':{}}
                (root/'runtime-manifest').mkdir();(root/'runtime-manifest/bootstrap-runtime.json').write_text(json.dumps(manifest))
                with patch('core.modular_runtime.subprocess.Popen',side_effect=AssertionError('must not execute')):
                    result=verify_runtime(root=root,data_root=root.parent/'modular-extra-data')
                self.assertEqual(result['status'],'FAIL');self.assertIn('unmanifested modular file',result['error'])

    def test_modular_polish_passes_gate_and_corrupt_file_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = 'tools/RVC/RVC-beta-v2-0618/runtime'
            (root / 'LIGHTWEIGHT_RELEASE').write_text('AI Music Processor Lightweight Release v1\n', encoding='utf-8')
            listed = [
                'AI_Music_Processor.pyw',
                'core/pipeline.py',
                'core/modular_runtime.py',
                runtime + '/python.exe',
                runtime + '/Lib/site-packages/numpy/__init__.py',
                runtime + '/Lib/site-packages/scipy/__init__.py',
                runtime + '/Lib/site-packages/soundfile.py',
                'runtime/bin/ffmpeg.exe',
                'runtime/bin/ffprobe.exe'
            ]
            files = []
            for name in listed:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                content = b'content of ' + name.encode()
                target.write_bytes(content)
                files.append({'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
            manifest = {
                'schema_version': 1,
                'profile': 'modular-polish-v1',
                'allowed_modes': ['polish'],
                'files': files,
                'python': {'executable': runtime + '/python.exe', 'version': '3.9.13'},
                'tools': {
                    'ffmpeg': {'version_line': 'ffmpeg version 6.0'},
                    'ffprobe': {'version_line': 'ffprobe version 6.0'}
                }
            }
            (root / 'runtime-manifest').mkdir()
            (root / 'runtime-manifest/bootstrap-runtime.json').write_text(json.dumps(manifest), encoding='utf-8')
            def mock_popen(command, *args, **kwargs):
                cmd_str = ' '.join(str(c) for c in command)
                mock_proc = Mock()
                mock_proc.poll.return_value = 0
                mock_proc.returncode = 0
                if 'python.exe' in cmd_str:
                    modules = {
                        'numpy': str(root / runtime / 'Lib/site-packages/numpy/__init__.py'),
                        'scipy': str(root / runtime / 'Lib/site-packages/scipy/__init__.py'),
                        'soundfile': str(root / runtime / 'Lib/site-packages/soundfile.py')
                    }
                    out = json.dumps({'version': '3.9.13', 'modules': modules}).encode('utf-8')
                    mock_proc.communicate.return_value = (out, b'')
                elif 'ffmpeg.exe' in cmd_str:
                    mock_proc.communicate.return_value = (b'ffmpeg version 6.0\nline2', b'')
                elif 'ffprobe.exe' in cmd_str:
                    mock_proc.communicate.return_value = (b'ffprobe version 6.0\nline2', b'')
                else:
                    mock_proc.communicate.return_value = (b'', b'')
                return mock_proc

            with patch('core.modular_runtime.subprocess.Popen', side_effect=mock_popen):
                result = verify_runtime(root=root, data_root=root.parent / 'modular-data')
                self.assertEqual(result['status'], 'PASS')
                self.assertEqual(result['allowed_modes'], ['polish'])
                self.assertEqual(result['profile'], 'modular-polish-v1')
                orig_len = (root / 'core/pipeline.py').stat().st_size
                (root / 'core/pipeline.py').write_bytes(b'X' * orig_len)
                result_corrupt = verify_runtime(root=root, data_root=root.parent / 'modular-data')
                self.assertEqual(result_corrupt['status'], 'FAIL')
                self.assertIn('modular SHA mismatch', result_corrupt['error'])
