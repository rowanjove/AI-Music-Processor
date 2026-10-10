import importlib.machinery,importlib.util,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


class VersionUIGuardsTests(unittest.TestCase):
    def test_component_archive_import_rechecks_after_file_dialog(self):
        from unittest.mock import patch
        app=self.app()
        from types import SimpleNamespace
        app.pipeline=SimpleNamespace(storage=SimpleNamespace(list_jobs=lambda *args:[]))
        def selected(**kwargs):
            app.cache_collect_running=True
            return 'current.ampkg'
        app.log=lambda message:None
        app._refresh_component_mutation_controls=lambda:None
        with patch.object(self.ui.filedialog,'askopenfilename',side_effect=selected):
            app.import_component_package()
        self.assertIn('缓存',app.status.text)
        self.assertFalse(app.component_install_running)

    @classmethod
    def setUpClass(cls):
        loader=importlib.machinery.SourceFileLoader('version_ui_guard_fixture',str(ROOT/'AI_Music_Processor.pyw'))
        spec=importlib.util.spec_from_loader(loader.name,loader);cls.ui=importlib.util.module_from_spec(spec)
        loader.exec_module(cls.ui)
    def app(self):
        app=object.__new__(self.ui.AIMusicProcessorApp)
        app.status=type('Status',(),{'config':lambda obj,**kwargs:setattr(obj,'text',kwargs['text'])})()
        app.is_running=False;app.component_install_running=False
        app._start_background_worker=lambda *args,**kwargs:self.fail('busy operation dispatched a worker')
        return app
    def test_update_rechecks_all_maintenance_flags_before_dispatch(self):
        for flag in ('component_install_running','component_size_running','cache_collect_running',
                     'benchmark_running','separator_experiment_running','version_update_running',
                     'model_import_running','new_install_running'):
            with self.subTest(flag=flag):
                app=self.app();setattr(app,flag,True)
                app._start_version_operation('source','target')
                self.assertTrue(app.status.text)
                if flag!='version_update_running':self.assertFalse(app.__dict__.get('version_update_running',False))
    def test_scan_blocks_update_and_candidate_experiment_blocks_job_start(self):
        app=self.app();app.scan_thread=type('Scan',(),{'is_alive':lambda _:True})()
        app._start_version_operation('source','target');self.assertIn('扫描',app.status.text)
        app=self.app();app.separator_experiment_running=True
        app.start();self.assertIn('候选分离实验',app.status.text)
    def test_finished_scan_with_unconsumed_events_still_blocks_collection(self):
        app=self.app();app.scan_thread=type('FinishedScan',(),{'is_alive':lambda _:False})()
        app._start_cache_collect({'quota_bytes':1,'ttl_days':None},False,[])
        self.assertIn('扫描',app.status.text)
        self.assertFalse(app.__dict__.get('cache_collect_running',False))
        self.assertIn('接收',app._audio_pipeline_busy_reason())
    def test_defensive_insert_does_not_add_source_after_collection_snapshot(self):
        app=self.app();app.cache_collect_running=True
        app._insert_pending_paths(['producer/.work/original.wav'])
        self.assertIn('重新扫描',app.status.text)
        self.assertTrue(app.scan_results_rejected)
    def test_changed_runtime_blocks_all_shared_audio_entry_guards(self):
        app=self.app();app.runtime_restart_required=True
        self.assertIn('重新启动',app._audio_pipeline_busy_reason())
        app.run_benchmark();self.assertIn('重新启动',app.status.text)
        app.start();self.assertIn('重新启动',app.status.text)
