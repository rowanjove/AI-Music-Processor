import tempfile,unittest
from pathlib import Path
from core.storage import JobStore,JobBusyError
from core.cache_manager import CacheManager


class HistoryVisibilityTests(unittest.TestCase):
    def test_hide_restore_keeps_original_inputs_and_job_evidence(self):
        with tempfile.TemporaryDirectory() as folder,JobStore(Path(folder)/'jobs.db') as store:
            root=Path(folder);source=root/'source.wav';source.write_bytes(b'original')
            project=root/'producer';(project/'.work').mkdir(parents=True)
            producer=store.create_job(str(source),'polish',project_dir=str(project));store.update_job(producer,status='DONE')
            cache=project/'.work'/'input.wav';cache.write_bytes(b'input')
            consumer=store.create_job(str(cache),'polish')
            store.set_history_hidden(consumer)
            self.assertTrue(store.get_job(consumer)['hidden'])
            self.assertNotIn(consumer,[x['id'] for x in store.list_jobs(include_hidden=False)])
            self.assertEqual(CacheManager(store,root).collect(1,None)['reclaimed_bytes'],0)
            store.set_history_hidden(consumer,False)
            self.assertIn(consumer,[x['id'] for x in store.list_jobs(include_hidden=False)])
            with store.execution(consumer),self.assertRaises(JobBusyError):store.set_history_hidden(consumer)
            self.assertEqual(source.read_bytes(),b'original');self.assertEqual(cache.read_bytes(),b'input')
