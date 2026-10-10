import os,sys,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.pipeline_support import _relocate_cached_outputs
from core.pipeline import AudioPipeline
from core.storage import JobStore


class CacheMaterializationTests(unittest.TestCase):
    def test_partial_copy_failure_keeps_no_target_or_source_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'old';target=root/'new'
            (source/'.work').mkdir(parents=True)
            files=[source/'.work'/name for name in ('a.wav','b.wav')]
            for p in files:p.write_bytes(p.name.encode())
            original=shutil.copy2
            def fail_second(a,b):
                if Path(a).name=='b.wav':raise OSError('injected disk failure')
                return original(a,b)
            with patch('core.pipeline_support.shutil.copy2',side_effect=fail_second):
                with self.assertRaises(OSError):
                    _relocate_cached_outputs({'wav':[str(p) for p in files]},str(source),str(target))
            self.assertEqual([p for p in target.rglob('*') if p.is_file()],[])
            self.assertEqual([p.read_bytes() for p in files],[b'a.wav',b'b.wav'])

    def test_commit_failure_restores_existing_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'old';target=root/'new'
            for d in (source/'.work',target/'.work'):d.mkdir(parents=True)
            files=[source/'.work'/name for name in ('a.wav','b.wav')]
            for p in files:p.write_bytes(b'new');(target/'.work'/p.name).write_bytes(b'original')
            original=os.replace
            def fail_second(a,b):
                if str(a).endswith('.tmp') and Path(b).name=='b.wav':raise OSError('injected commit failure')
                return original(a,b)
            with patch('core.pipeline_support.os.replace',side_effect=fail_second):
                with self.assertRaises(OSError):
                    _relocate_cached_outputs({'wav':[str(p) for p in files]},str(source),str(target))
            self.assertEqual([p.read_bytes() for p in sorted((target/'.work').iterdir())],[b'original',b'original'])

    def test_failed_cache_copy_executes_stage_and_source_gc_cannot_break_new_output(self):
        with tempfile.TemporaryDirectory() as folder,JobStore(Path(folder)/'jobs.db') as store:
            root=Path(folder);source=root/'source.wav';source.write_bytes(b'source')
            old=root/'old';new=root/'new';(old/'.work').mkdir(parents=True);(new/'.work').mkdir(parents=True)
            first=store.create_job(str(source),'polish',project_dir=str(old))
            second=store.create_job(str(source),'polish',project_dir=str(new))
            pipeline=AudioPipeline(storage=store,log_callback=lambda m:None)
            def render(directory):
                output=directory/'.work'/'result.wav';output.write_bytes(b'rendered');return {'wav':str(output)}
            with patch.object(pipeline,'_cache_parameters',side_effect=lambda s,p:dict(p)):
                with store.execution(first):pipeline._run_stage(first,'Repair',{'input':str(source)},{},lambda:render(old),.2)
                with store.execution(second),patch('core.pipeline_support.shutil.copy2',side_effect=OSError('injected')):
                    result=pipeline._run_stage(second,'Repair',{'input':str(source)},{},lambda:render(new),.2)
            self.assertEqual(store.get_stage(second,'Repair')['parameters']['_eta_execution_kind'],'executed')
            shutil.rmtree(old)
            self.assertEqual(Path(result['wav']).read_bytes(),b'rendered')
            try:
                Path(result['wav']).resolve().relative_to(new.resolve())
                _is_rel = True
            except ValueError:
                _is_rel = False
            self.assertTrue(_is_rel)


if __name__=='__main__':unittest.main()
