import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import soundfile as sf
from core.pipeline import AudioPipeline
from core.storage import JobStore,sha256_file


class ExportRepairTests(unittest.TestCase):
    def source(self,root):
        path=root/'原输入.wav'
        sf.write(path,.12*np.sin(2*np.pi*220*np.arange(48000*4)/48000),48000,subtype='PCM_24')
        return path

    def test_actual_corrupt_mp3_is_rebuilt_once_and_rechecked(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=self.source(root);original=sha256_file(source)
            with JobStore(root/'jobs.db') as store:
                pipeline=AudioPipeline(storage=store,log_callback=lambda _:None)
                render=pipeline._master;calls=[]
                def damage_once(*args,**kwargs):
                    result=render(*args,**kwargs);calls.append(result)
                    if len(calls)==1:Path(result['mp3']).write_bytes(b'broken MP3 from initial export')
                    return result
                with patch.object(pipeline,'_master',damage_once):
                    result=pipeline.process_file(source,mode='polish',device='cpu',detailed_analysis=False,
                        output_formats=['wav','mp3'],custom_output_dir=str(root/'output'))
                self.assertEqual(len(calls),2)
                job=store.list_jobs()[0];self.assertEqual(job['status'],'DONE')
                self.assertEqual(store.get_stage(job['id'],'Master')['attempt'],2)
                self.assertEqual(store.get_stage(job['id'],'QC')['attempt'],2)
                self.assertIn(result['qc']['status'],('PASS','WARN'))
                path=Path(result['output_paths']['reports_dir'])/'export_repair.json'
                repair=json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(repair['status'],'QC_RECHECKED');self.assertEqual(repair['retry_limit'],1)
                self.assertEqual(repair['broken_exports'][0]['role'],'mp3')
                self.assertTrue(any(Path(x['backup']).read_bytes()==b'broken MP3 from initial export' for x in repair['preserved_files']))
                self.assertEqual(sha256_file(source),original)
                from core.pipeline_support import _validate_outputs
                for name in ('Master','QC'):
                    stage=store.get_stage(job['id'],name)
                    self.assertTrue(_validate_outputs(stage['outputs'],stage['output_hashes']))

    def test_sound_quality_failure_does_not_trigger_reexport(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=self.source(root)
            with JobStore(root/'jobs.db') as store:
                pipeline=AudioPipeline(storage=store,log_callback=lambda _:None)
                with patch.object(pipeline,'_master',wraps=pipeline._master) as renderer, \
                     patch.object(pipeline,'_qc',side_effect=RuntimeError('QC FAIL: sound-quality finding')):
                    with self.assertRaisesRegex(RuntimeError,'sound-quality'):
                        pipeline.process_file(source,mode='polish',detailed_analysis=False,
                            output_formats=['wav','mp3'],custom_output_dir=str(root/'output'))
                self.assertEqual(renderer.call_count,1)
                self.assertEqual(store.list_jobs()[0]['status'],'FAILED')
