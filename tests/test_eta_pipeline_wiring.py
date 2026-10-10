import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.pipeline import AudioPipeline
from core.storage import JobStore


IDENTITY = {
    "backend": "pipeline-test:abc", "hardware": "CPU:test",
    "model_sha256": "none", "runtime_sha256": "a" * 64,
    "profile_sha256": "b" * 64,
}


class ETAPipelineWiringTests(unittest.TestCase):
    def test_runtime_hash_and_history_snapshot_are_memoized_only_within_one_run(self):
        with tempfile.TemporaryDirectory() as folder, JobStore(Path(folder) / "state.db") as store:
            pipeline = AudioPipeline(storage=store, log_callback=lambda _message: None)
            with patch("core.pipeline.sha256_file", return_value="c" * 64) as hash_runtime, \
                 patch.object(pipeline, "_eta_history", return_value=[]) as read_history:
                pipeline._begin_eta_run()
                parameters = {"dependency_hashes": {"source.py": "a" * 64}}
                first = pipeline._eta_identity("Master", parameters)
                second = pipeline._eta_identity("Master", dict(parameters, preset="natural"))
                self.assertEqual(first["runtime_sha256"], "c" * 64)
                self.assertEqual(second["runtime_sha256"], "c" * 64)
                self.assertEqual(hash_runtime.call_count, 1)
                pipeline._eta_history_for("polish", "Master", first)
                pipeline._eta_history_for("polish", "Master", first)
                self.assertEqual(read_history.call_count, 1)

                pipeline._begin_eta_run()
                pipeline._eta_identity("Master", parameters)
                self.assertEqual(hash_runtime.call_count, 2)
                pipeline._eta_history_for("polish", "Master", first)
                self.assertEqual(read_history.call_count, 2)

    def test_executed_stage_history_is_persisted_and_cache_hits_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder, JobStore(Path(folder) / "state.db") as store:
            root = Path(folder)
            source = root / "source.wav"
            source.write_bytes(b"source")
            first_project = root / "first"
            second_project = root / "second"
            first_project.mkdir(); second_project.mkdir()
            (first_project / 'master').mkdir()
            first_output = first_project / "master" / "master.wav"
            first_job = store.create_job(str(source), "polish", project_dir=str(first_project))
            pipeline = AudioPipeline(storage=store, log_callback=lambda _message: None)
            pipeline._eta_audio_duration = 12.0
            def render_first():
                first_output.write_bytes(b"rendered")
                return {"wav": str(first_output)}
            with store.execution(first_job), \
                 patch.object(pipeline, "_cache_parameters", side_effect=lambda _stage, params: dict(params)), \
                 patch.object(pipeline, "_eta_identity", return_value=IDENTITY):
                pipeline._run_stage(first_job, "Master", {"input": str(source)}, {"preset": "natural"},
                                    render_first, .8)
            first_stage = store.get_stage(first_job, "Master")
            self.assertEqual(first_stage["parameters"]["_eta_execution_kind"], "executed")
            self.assertEqual(first_stage["parameters"]["_eta_audio_duration"], 12.0)
            self.assertEqual(first_stage["parameters"]["_eta_identity"], IDENTITY)
            matching = [row for row in pipeline._eta_history() if row["stage_name"] == "Master"]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["execution_kind"], "executed")

            second_job = store.create_job(str(source), "polish", project_dir=str(second_project))
            with store.execution(second_job), \
                 patch.object(pipeline, "_cache_parameters", side_effect=lambda _stage, params: dict(params)), \
                 patch.object(pipeline, "_eta_identity", return_value=IDENTITY):
                reused = pipeline._run_stage(second_job, "Master", {"input": str(source)}, {"preset": "natural"},
                                             lambda: self.fail("cache hit must not execute the renderer"), .8)
            self.assertEqual(Path(reused["wav"]).read_bytes(), b"rendered")
            second_stage = store.get_stage(second_job, "Master")
            self.assertEqual(second_stage["parameters"]["_eta_execution_kind"], "cache_hit")
            matching = [row for row in pipeline._eta_history() if row["stage_name"] == "Master"]
            self.assertEqual(len(matching), 2)
            self.assertEqual(sum(row["execution_kind"] == "executed" for row in matching), 1)


if __name__ == "__main__":
    unittest.main()
