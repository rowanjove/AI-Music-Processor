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
