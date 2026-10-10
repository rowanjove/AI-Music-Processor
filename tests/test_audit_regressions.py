"""Reproduce audit failures against isolated data, without AI inference."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from core.storage import JobStore, JobBusyError, JobOwnershipError, CorruptJobError, sha256_file
from core.cache_manager import CacheManager
from core.job_spec import freeze, validate, digest
from core.pipeline import AudioPipeline


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='审查回归 ')
        self.root = Path(self.tmp.name)
        self.source = self.root/'source.wav'; self.source.write_bytes(b'source')
        self.store = JobStore(self.root/'state.db')
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def job(self, name='project', status='DONE'):
        project = self.root/name; (project/'.work').mkdir(parents=True,exist_ok=True)
        job = self.store.create_job(str(self.source),'polish',project_dir=str(project))
        self.store.update_job(job,status=status)
        return job, project

    def test_cross_process_claim_and_crash_release(self):
        job,_ = self.job()
        bootstrap = 'import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parent.parent))+');'
        script = bootstrap+"from core.storage import JobStore,JobBusyError; s=JobStore(sys.argv[1]);\ntry:\n with s.execution(sys.argv[2]): print('ACQUIRED')\nexcept JobBusyError: print('BUSY')\nfinally:s.close()"
        with self.store.execution(job):
            completed = subprocess.run([sys.executable,'-c',script,self.store.db_path,job],capture_output=True,text=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertIn('BUSY',completed.stdout)
            other = JobStore(self.store.db_path)
            try:
                with self.assertRaises(JobOwnershipError): other.update_job(job,status='FAILED')
            finally: other.close()
        crash = bootstrap+"from core.storage import JobStore;import os;s=JobStore(sys.argv[1]);c=s.execution(sys.argv[2]);c.__enter__();os._exit(9)"
        subprocess.run([sys.executable,'-c',crash,self.store.db_path,job],capture_output=True)
        with self.store.execution(job): self.store.update_job(job,status='DONE')
        self.assertIsNone(self.store.get_job(job)['owner_token'])

    def test_stale_owner_cannot_commit(self):
        job,_ = self.job()
        with self.store.execution(job):
            with self.store.transaction() as conn: conn.execute('UPDATE jobs SET owner_token=? WHERE id=?',('new-owner',job))
            with self.assertRaises(JobOwnershipError): self.store.upsert_stage(job,'Clean',status='DONE')
        self.assertEqual(self.store.get_job(job)['owner_token'],'new-owner')

    def test_corrupt_parameters_fail_but_history_stays_readable(self):
        job,project = self.job()
        sentinel = project/'master.wav'; sentinel.write_bytes(b'original')
        with self.store.transaction() as conn: conn.execute('UPDATE jobs SET parameters_json=? WHERE id=?',('{bad',job))
        with self.assertRaises(CorruptJobError): self.store.get_job(job)
        self.assertIn('snapshot_error',self.store.list_jobs()[0])
        pipeline = AudioPipeline(storage=self.store,log_callback=lambda _:None)
        with self.assertRaises(CorruptJobError): pipeline.process_file(str(self.source),resume_job_id=job)
        self.assertEqual(sentinel.read_bytes(),b'original')

    def parameters(self):
        return dict(clean_preset='light',master_preset='natural',rvc_model_key='demo',pitch_shift=0,
                    f0_method='harvest',index_rate=.65,generate_midi=False,output_formats=['wav'],output_sample_rate=48000,bpm=120)

    def test_effective_profile_frozen_and_artifact_change_blocks(self):
        pth=self.root/'model.pth';pth.write_bytes(b'original-model')
        model=dict(name='demo',pth=str(pth),index=None,profile=dict(protect=.1,rms_mix_rate=.2,filter_radius=1))
        with patch('core.job_spec.get_available_rvc_models',return_value={'demo':model}): spec=freeze(self.parameters(),'voice_conversion')
        model['profile']['protect']=.4
        self.assertEqual(validate(spec,'voice_conversion')['effective_rvc']['profile']['protect'],.1)
        pth.write_bytes(b'changed-model')
        with self.assertRaises(CorruptJobError): validate(spec,'voice_conversion')
        with self.assertRaises(CorruptJobError): validate(self.parameters(),'polish')

    def test_snapshot_wrong_types_and_checksum_fail(self):
        spec=freeze(self.parameters(),'polish')
        for key,value in [('generate_midi','false'),('pitch_shift',True),('bpm',float('nan'))]:
            bad=copy.deepcopy(spec);bad[key]=value;bad['spec_hash']=digest(bad)
            with self.assertRaises(CorruptJobError):validate(bad,'polish')
        bad=copy.deepcopy(spec);bad['effective_clean']['filter']='anull'
        with self.assertRaises(CorruptJobError):validate(bad,'polish')

    def test_gc_lru_pins_resume_dependencies_and_preserves_products(self):
        job,project=self.job()
        paths=[]
        for i in range(3):
            path=project/'.work'/f'{i}.wav';path.write_bytes(b'x'*100);os.utime(path,(100+i,100+i));paths.append(path)
            self.store.put_cache(str(i),'Clean','input',{}, {'cleaned':str(path)}, {'cleaned':sha256_file(path)})
            with self.store.transaction() as conn:conn.execute('UPDATE cache_entries SET last_used_at=? WHERE cache_key=?',(f'2000-01-0{i+1}T00:00:00+00:00',str(i)))
        failed,other=self.job('failed','FAILED');(other/'.work'/'needed.wav').write_bytes(b'x'*100)
        final=project/'master.wav';final.write_bytes(b'product')
        gc=CacheManager(self.store,self.root)
        preview=gc.collect(150,None,True)
        self.assertTrue(all(p.exists() for p in paths));self.assertEqual(preview['pinned_bytes'],100)
        result=gc.collect(150,None)
        self.assertEqual(result['reclaimed_bytes'],300)
        self.assertTrue(final.exists());self.assertTrue((other/'.work'/'needed.wav').exists())
        self.assertEqual(self.store.list_cache(),[])

    def test_gc_does_not_delete_active_or_outside_root(self):
        job,project=self.job();file=project/'.work'/'x';file.write_bytes(b'cache')
        with self.store.execution(job):
            result=CacheManager(self.store,self.root).collect(1,None)
            self.assertTrue(file.exists());self.assertEqual(result['reclaimed_bytes'],0)
        self.assertFalse(CacheManager(self.store,self.root)._safe(self.source))

    def test_gc_protects_cross_project_sources_and_locked_consumers(self):
        job,project=self.job();source=project/'.work'/'original.wav';source.write_bytes(b'source')
        consumer=self.store.create_job(str(source),'polish',project_dir=str(self.root/'consumer'))
        gc=CacheManager(self.store,self.root)
        self.assertEqual(gc.collect(1,None)['reclaimed_bytes'],0)
        orphan=project/'.work'/'new_input.wav';orphan.write_bytes(b'new source')
        self.assertEqual(gc.collect(1,None,protected_paths=[str(orphan)])['reclaimed_bytes'],0)
        with self.store.project_lock(str(project)):
            self.assertEqual(gc.collect(1,None)['reclaimed_bytes'],0)

    def test_model_snapshot_survives_original_replacement(self):
        pth=self.root/'model.pth';pth.write_bytes(b'original-model')
        model=dict(name='demo',pth=str(pth),index=None,profile=dict(protect=.1,rms_mix_rate=.2,filter_radius=1))
        with patch('core.job_spec.get_available_rvc_models',return_value={'demo':model}):
            spec=freeze(self.parameters(),'voice_conversion',artifact_root=str(self.root/'artifacts'))
        pth.write_bytes(b'replacement-model')
        frozen=validate(spec,'voice_conversion')['effective_rvc']['pth']
        self.assertEqual(Path(frozen).read_bytes(),b'original-model')

    def test_gc_refreshes_consumer_created_after_scan(self):
        _,project=self.job()
        source=project/'.work'/'late.wav';source.write_bytes(b'original input')
        manager=CacheManager(self.store,self.root)
        scanned=list(manager._jobs())
        def scan():
            yield from scanned
            self.store.create_job(str(source),'polish',project_dir=str(self.root/'late-consumer'))
        with patch.object(manager,'_jobs',scan):
            result=manager.collect(1,None)
        self.assertEqual(result['reclaimed_bytes'],0)
        self.assertEqual(source.read_bytes(),b'original input')

    def test_gc_atomic_reference_check_rejects_late_deleted_source(self):
        import threading
        _,project=self.job()
        source=project/'.work'/'race.wav';source.write_bytes(b'cache')
        started=threading.Event(); finished=threading.Event(); outcome=[]
        other=JobStore(self.store.db_path)
        def create():
            started.set()
            try:other.create_job(str(source),'polish')
            except Exception as exc:outcome.append(exc)
            finally:finished.set()
        thread=threading.Thread(target=create)
        unlink=Path.unlink
        def delete(path,*args,**kwargs):
            if path==source:
                thread.start();self.assertTrue(started.wait(2))
                if finished.wait(.1):
                    self.assertEqual(len(outcome),1)
                    self.assertIsInstance(outcome[0],JobBusyError)
            return unlink(path,*args,**kwargs)
        try:
            with patch.object(Path,'unlink',delete):
                result=CacheManager(self.store,self.root).collect(1,None)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result['reclaimed_bytes'],5)
            self.assertEqual(len(outcome),1)
            self.assertIsInstance(outcome[0],(FileNotFoundError,JobBusyError))
        finally:
            thread.join(5);other.close()

    def test_gc_preserves_source_registered_in_independent_database(self):
        _,project=self.job()
        source=project/'.work'/'cross-db.wav';source.write_bytes(b'original')
        with JobStore(self.root/'independent.db') as other:
            with other.project_lock(project):
                other.create_job(str(source),'polish')
        result=CacheManager(self.store,self.root).collect(1,None)
        self.assertEqual(result['reclaimed_bytes'],0)
        self.assertEqual(source.read_bytes(),b'original')

    def test_cache_source_alias_and_cross_database_delete_lock(self):
        _,project=self.job()
        source=project/'.WORK'/'alias.wav'
        source.parent.mkdir(exist_ok=True);source.write_bytes(b'original')
        with JobStore(self.root/'independent.db') as other:
            import threading
            outcome=[]
            def create():
                try:other.create_job(str(source),'polish')
                except Exception as exc:outcome.append(exc)
            with self.store.project_lock(project):
                thread=threading.Thread(target=create);thread.start();thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(outcome),1)
                self.assertIsInstance(outcome[0],JobBusyError)
                source.unlink()
            with self.assertRaises(FileNotFoundError):other.create_job(str(source),'polish')

    @unittest.skipUnless(os.name=='nt','Windows junction regression')
    def test_gc_preserves_cross_database_input_through_junction_alias(self):
        _,project=self.job()
        source=project/'.work'/'original.wav';source.write_bytes(b'original')
        alias=self.root/'producer-alias'
        result=subprocess.run([os.environ['COMSPEC'],'/c','mklink','/J',str(alias),str(project)],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        try:
            with JobStore(self.root/'independent-alias.db') as other:
                other.create_job(str(alias/'.work'/'original.wav'),'polish')
            collected=CacheManager(self.store,self.root).collect(1,None)
            self.assertEqual(collected['reclaimed_bytes'],0)
            self.assertEqual(source.read_bytes(),b'original')
        finally:
            # Remove only the known junction itself, never recurse into it.
            os.rmdir(alias)

    @unittest.skipUnless(os.name=='nt','Windows junction regression')
    def test_gc_pending_alias_and_legacy_same_database_reference_are_protected(self):
        _,project=self.job()
        source=project/'.work'/'original.wav';source.write_bytes(b'original')
        alias=self.root/'producer-alias'
        result=subprocess.run([os.environ['COMSPEC'],'/c','mklink','/J',str(alias),str(project)],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        try:
            aliased=alias/'.work'/'original.wav'
            self.assertEqual(CacheManager(self.store,self.root).collect(1,None,protected_paths=[str(aliased)])['reclaimed_bytes'],0)
            consumer=self.store.create_job(str(self.source),'polish')
            # Historical source references predate the persistent pin protocol.
            with self.store.transaction() as conn:
                conn.execute('UPDATE jobs SET source_path=? WHERE id=?',(str(aliased),consumer))
            self.assertEqual(CacheManager(self.store,self.root).collect(1,None)['reclaimed_bytes'],0)
            self.assertEqual(source.read_bytes(),b'original')
        finally:os.rmdir(alias)

    def test_gc_delete_failure_retains_index(self):
        job,project=self.job();file=project/'.work'/'x';file.write_bytes(b'cache')
        self.store.put_cache('key','Ingest','x',{}, {'standard_wav':str(file)}, {'standard_wav':sha256_file(file)})
        with patch.object(Path,'unlink',side_effect=PermissionError('locked file')):
            result=CacheManager(self.store,self.root).collect(1,None)
        self.assertTrue(result['errors']);self.assertIsNotNone(self.store.get_cache('key'))

    def test_license_subjects_unique_and_incomplete_ready_unknown(self):
        first=self.store.put_license('model_a','b',commercial_status='Ready')
        second=self.store.put_license('model','a_b',commercial_status='Ready')
        self.assertNotEqual(first,second)
        self.assertEqual(self.store.list_licenses('model_a','b')[0]['commercial_status'],'Unknown')
        self.store.put_license('model_a','b',license_id='another-id',commercial_status='Warning')
        self.assertEqual(len(self.store.list_licenses('model_a','b')),1)
        with self.assertRaises(ValueError):self.store.put_license('other','subject',license_id=second)

    def test_missing_requested_flac_is_not_skipped(self):
        pipeline=AudioPipeline(storage=self.store)
        project=self.root/'qc';project.mkdir()
        def qc(*args,**kwargs):
            self.assertEqual(args[-1],[str(self.root/'missing.flac')])
            raise FileNotFoundError(args[-1][0])
        with patch.object(pipeline,'_run_qc',side_effect=qc):
            with self.assertRaises(FileNotFoundError):pipeline._qc('wav',None,'x','polish','N/A',0,3,dict(target_lufs=-11,true_peak=-1),None,str(project),str(project),str(self.root/'missing.flac'))

    def test_disk_second_budget_uses_decoded_duration(self):
        with patch('core.pipeline.probe_metadata',return_value={'duration':300}),patch('core.pipeline.shutil.disk_usage',return_value=type('D',(),{'free':100*1024**2})()):
            with self.assertRaises(OSError):AudioPipeline._disk_preflight(str(self.source),str(self.root))

    def test_unrelated_module_does_not_invalidate_clean_cache(self):
        from core.cache_fingerprint import enrich
        a=enrich('Clean',{'preset':'light'})
        self.assertFalse(any('diagnostics.py' in key or 'infer_batch_rvc.py' in key for key in a['dependency_hashes']))
        self.assertTrue(any('cleaner.py' in key for key in a['dependency_hashes']))

    def test_stage_commit_rollback_keeps_cache_and_ledger_consistent(self):
        job,project=self.job(status='PENDING');file=project/'.work'/'result.wav'
        pipeline=AudioPipeline(storage=self.store,log_callback=lambda _:None)
        def runner():file.write_bytes(b'valid stage bytes');return {'cleaned':str(file)}
        with self.store.execution(job),patch.object(self.store,'register_output',side_effect=OSError('injected ledger write failure')):
            with self.assertRaises(OSError):pipeline._run_stage(job,'Clean',str(self.source),{},runner,.3)
        self.assertEqual(self.store.get_stage(job,'Clean')['status'],'FAILED')
        self.assertEqual(self.store.list_cache(),[]);self.assertEqual(self.store.list_outputs(job),[])

    def test_resource_budget_refuses_unbounded_whole_audio(self):
        from core.resource_limits import require_processing_budget
        require_processing_budget(300,'voice_conversion')
        with self.assertRaises(MemoryError):require_processing_budget(1500,'voice_conversion')
        with self.assertRaises(ValueError):require_processing_budget(1801,'polish')

    def test_complete_model_license_is_not_complete_project_license(self):
        from core.reporting import license_manifest
        evidence=dict(name='demo',license_name='user recorded terms',source_url='https://example.invalid/evidence',author='fixture',note='fixture evidence')
        self.store.put_license('model','demo',commercial_status='Ready',**evidence)
        manifest=license_manifest(self.store,'voice_conversion','demo','demo',str(self.source),sha256_file(self.source))
        self.assertEqual(manifest['commercial_status'],'Unknown')
        self.assertEqual(manifest['rvc_model']['commercial_status'],'Ready')


if __name__=='__main__':unittest.main()
