import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.pipeline import AudioPipeline
from core.storage import JobStore


class StageCommitIdentityTests(unittest.TestCase):
    def test_changed_input_cannot_poison_the_cache(self):
        with tempfile.TemporaryDirectory() as folder, JobStore(Path(folder) / "state.db") as store:
            source = Path(folder) / "source.wav"
            source.write_bytes(b"before")
            output = Path(folder) / "result.wav"
            job_id = store.create_job(str(source), "polish", project_dir=folder)
            pipeline = AudioPipeline(storage=store, log_callback=lambda text: None)
            def render():
                source.write_bytes(b"after")
                output.write_bytes(b"rendered")
                return {"wav": str(output)}
            with store.execution(job_id), patch.object(pipeline, "_cache_parameters", side_effect=lambda stage, params: dict(params)):
                with self.assertRaisesRegex(RuntimeError, "拒绝提交缓存"):
                    pipeline._run_stage(job_id, "Repair", {"input": str(source)}, {}, render, .2)
            self.assertEqual(store.get_stage(job_id, "Repair")["status"], "FAILED")
            self.assertEqual(store.list_cache(), [])

    def test_changed_dependency_cannot_poison_the_cache(self):
        with tempfile.TemporaryDirectory() as folder, JobStore(Path(folder) / "state.db") as store:
            source = Path(folder) / "source.wav"
            source.write_bytes(b"source")
            output = Path(folder) / "result.wav"
            job_id = store.create_job(str(source), "polish", project_dir=folder)
            pipeline = AudioPipeline(storage=store, log_callback=lambda text: None)
            def render():
                output.write_bytes(b"rendered")
                return {"wav": str(output)}
            with store.execution(job_id), patch.object(pipeline, "_cache_parameters",
                    side_effect=[{"dependency_hashes": {"model": "old"}}, {"dependency_hashes": {"model": "new"}}]):
                with self.assertRaisesRegex(RuntimeError, "拒绝提交缓存"):
                    pipeline._run_stage(job_id, "Repair", {"input": str(source)}, {}, render, .2)
            self.assertEqual(store.list_cache(), [])


if __name__ == "__main__":
    unittest.main()
