import copy
import queue
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core.history_rerun import (clone_history_spec, create_history_rerun_job,
                                preflight_queue_items, resolve_queue_item)
from core.job_spec import digest, freeze
from core.separator_routing import hp2_profile
from core.storage import CorruptJobError, JobStore, sha256_path


class HistoryRerunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="history-rerun-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source.wav"
        self.source.write_bytes(b"current source bytes")
        self.model = self.root / "model.pth"
        self.model.write_bytes(b"frozen model bytes")
        self.store = JobStore(self.root / "jobs.db")
        self._model_patch = patch("core.job_spec.get_available_rvc_models", return_value={
            "test": {
                "name": "test", "pth": str(self.model), "index": None,
                "profile": {"protect": 0.2, "filter_radius": 3, "rms_mix_rate": 0.25},
            }
        })
        self._model_patch.start()

    def tearDown(self):
        self._model_patch.stop()
        self.store.close()
        self.temp.cleanup()

    def make_spec(self, *, legacy_int16=False, include_mix_reverb=True):
        params = {
            "clean_preset": "light", "master_preset": "natural",
            "rvc_model_key": "test", "pitch_shift": 7, "f0_method": "harvest",
            "index_rate": 0.4, "generate_midi": False,
            "output_formats": ["wav"], "output_sample_rate": 48000, "bpm": 120,
            "separator_profile": hp2_profile(), "source_hint": "auto",
            "source_preclean_enabled": False,
        }
        spec = freeze(params, "voice_conversion")
        spec["effective_rvc"]["output_precision"] = "legacy_int16" if legacy_int16 else "float32"
        if not include_mix_reverb:
            spec.pop("mix_reverb", None)
        spec["spec_hash"] = digest(spec)
        return spec

    def create_history(self, spec, *, mode="voice_conversion"):
        old_dir = self.root / ("old-project-%d" % len(self.store.list_jobs()))
        old_dir.mkdir()
        job_id = self.store.create_job(str(self.source), mode, spec,
                                       source_hash=sha256_path(self.source),
                                       project_dir=str(old_dir))
        return self.store.get_job(job_id), old_dir

    def test_clone_preserves_full_snapshot_and_expands_legacy_mix_without_mutating_old_job(self):
        spec = self.make_spec(legacy_int16=True, include_mix_reverb=False)
        job, _ = self.create_history(spec)
        self.store.upsert_stage(job["id"], "Mix", status="DONE",
                                parameters={"reverb": True})
        stages = self.store.list_stages(job["id"])

        cloned = clone_history_spec(job, stages)
        persisted_old = self.store.get_job(job["id"])["parameters"]
        self.assertEqual(cloned["effective_rvc"]["output_precision"], "legacy_int16")
        self.assertEqual(cloned["effective_rvc"]["profile"], spec["effective_rvc"]["profile"])
        self.assertTrue(cloned["mix_reverb"])
        self.assertEqual(cloned["spec_hash"], digest(cloned))
        self.assertNotEqual(cloned["spec_hash"], spec["spec_hash"])
        self.assertNotIn("mix_reverb", persisted_old)
        self.assertEqual(persisted_old, spec)

    def test_new_job_has_current_source_hash_unique_project_and_no_old_stages(self):
        spec = self.make_spec(legacy_int16=True)
        job, old_dir = self.create_history(spec)
        cloned = clone_history_spec(job, self.store.list_stages(job["id"]))
        result = create_history_rerun_job(
            self.store, str(self.source), job["mode"], cloned,
            output_root=str(self.root / "output"), original_project_dir=str(old_dir),
            now=datetime(2026, 10, 8, 12, 0, 0))

        fresh = self.store.get_job(result["job_id"])
        self.assertNotEqual(result["job_id"], job["id"])
        self.assertNotEqual(result["project_dir"], str(old_dir))
        self.assertTrue(Path(result["project_dir"]).is_dir())
        self.assertEqual(fresh["source_hash"], sha256_path(self.source))
        self.assertEqual(fresh["parameters"], cloned)
        self.assertEqual(self.store.list_stages(result["job_id"]), [])
        self.assertEqual(self.store.list_stages(job["id"]), [])

    def test_current_gui_mode_model_and_profile_do_not_replace_snapshot(self):
        spec = self.make_spec(legacy_int16=True)
        job, _ = self.create_history(spec)
        item = {"path": str(self.source), "source_mode": "voice_conversion",
                "frozen_spec": copy.deepcopy(job["parameters"]), "new_job_from_snapshot": True}
        defaults = {"mode": "polish", "rvc_model_key": "different", "pitch_shift": -2,
                    "separator_profile": {"mode": "dual"}}
        mode, resolved, is_frozen = resolve_queue_item(item, defaults, self.store)
        self.assertEqual(mode, "voice_conversion")
        self.assertTrue(is_frozen)
        self.assertEqual(resolved, spec)
        self.assertEqual(resolved["rvc_model_key"], "test")
        self.assertEqual(resolved["effective_rvc"]["output_precision"], "legacy_int16")
        self.assertEqual(resolved["effective_rvc"]["profile"]["protect"], 0.2)
        self.assertEqual(resolved["pitch_shift"], 7)

    def test_gui_start_preflights_frozen_item_without_reading_current_controls(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import AI_Music_Processor as ui

        class Widget:
            def __init__(self):
                self.calls = []
            def configure(self, **kwargs):
                self.calls.append(("configure", kwargs))
            def start(self, interval):
                self.calls.append(("start", interval))
            def set(self, *args):
                self.calls.append(("set", args))

        spec = self.make_spec(legacy_int16=True)
        job, old_dir = self.create_history(spec)
        item = {"path": str(self.source), "resume": None,
                "frozen_spec": copy.deepcopy(spec), "source_mode": "voice_conversion",
                "new_job_from_snapshot": True, "original_project_dir": str(old_dir)}
        app = object.__new__(ui.AIMusicProcessorApp)
        app.is_running = False
        app.component_install_running = False
        app.scan_thread = None
        app.pending = {"queue-key": item}
        app.pipeline = type("Pipeline", (), {"storage": self.store})()
        app.parameters = lambda: (_ for _ in ()).throw(AssertionError("current controls were read"))
        app._persist_settings = lambda *_args: (_ for _ in ()).throw(
            AssertionError("current settings were persisted"))
        app._rvc_model_run_guard = lambda *_args: (_ for _ in ()).throw(
            AssertionError("current model selection was checked"))
        app.status = Widget()
        app.tree = Widget()
        app.progress = Widget()
        app._refresh_component_mutation_controls = lambda: None
        app._arm_completion_for_batch = lambda _items: True
        app._try_save_queue = lambda: True
        app._start_background_worker = lambda target, args, daemon, name: (target, args, daemon, name)
        seen_modes = []

        def route(_registry, mode, profile=None):
            seen_modes.append((mode, profile))
            return {"modes": {mode: {"status": "AVAILABLE"}}}

        with patch("core.component_platform.build_registry", return_value={}), \
                patch("core.ux_flow.route_capability_report", side_effect=route):
            app.start()

        self.assertEqual(seen_modes[0][0], "voice_conversion")
        self.assertEqual(item["source_mode"], "voice_conversion")
        self.assertEqual(item["frozen_spec"], spec)
        self.assertTrue(app.is_running)

    def test_explicit_current_settings_action_is_the_only_history_snapshot_bypass(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import AI_Music_Processor as ui

        class Tree:
            def insert(self, *_args, **_kwargs):
                return "current-rerun"

        app = object.__new__(ui.AIMusicProcessorApp)
        app.is_running = False
        app.selected_job = lambda: {"id": "old", "source_path": str(self.source),
                                    "mode": "voice_conversion", "parameters": {"pitch_shift": 12}}
        current = {"mode": "polish", "clean_preset": "strong", "pitch_shift": -3}
        app.parameters = lambda: copy.deepcopy(current)
        app.tree = Tree()
        app.pending = {}
        app.pending_paths = set()
        app._persist_settings = lambda _params: None
        app._try_save_queue = lambda: True
        app.start = lambda: setattr(app, "started", True)

        app.rerun_history_current()

        item = app.pending["current-rerun"]
        self.assertTrue(item["current_parameters_explicit"])
        self.assertNotIn("frozen_spec", item)
        self.assertEqual(item["source_mode"], "polish")
        self.assertEqual(item["params"], current)
        self.assertTrue(app.started)

    def test_worker_creates_then_resumes_the_fresh_job_snapshot(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import AI_Music_Processor as ui
        from core.processes import Control

        class Storage:
            def get_job(self, job_id):
                return {"id": job_id, "status": "DONE"}
            def list_jobs(self, _limit):
                return []

        class Pipeline:
            def __init__(self):
                self.storage = Storage()
                self.call = None
            def process_file(self, source, **params):
                self.call = (source, params)
                return {"job_id": "fresh-job"}

        pipeline = Pipeline()
        app = object.__new__(ui.AIMusicProcessorApp)
        app.pipeline = pipeline
        app.events = queue.Queue()
        app.control = Control()
        app._sleep_guard_requested = False
        app.log = lambda _message: None
        item = {"path": str(self.source), "source_mode": "voice_conversion",
                "frozen_spec": self.make_spec(), "new_job_from_snapshot": True,
                "dispatch_id": "batch-id"}
        created = {"job_id": "fresh-job", "mode": "voice_conversion"}
        with patch("core.history_rerun.create_history_rerun_job", return_value=created) as create_job:
            app.work([("queue-key", item)])

        create_job.assert_called_once()
        self.assertEqual(pipeline.call[0], str(self.source))
        self.assertEqual(pipeline.call[1]["mode"], "voice_conversion")
        self.assertEqual(pipeline.call[1]["resume_job_id"], "fresh-job")
        self.assertIs(pipeline.call[1]["control"], app.control)
        self.assertEqual(set(pipeline.call[1]), {"mode", "resume_job_id", "control"})
        self.assertEqual(item["resume"], "fresh-job")

    def test_preflight_uses_each_queue_items_mode(self):
        calls = []

        def report(_registry, mode, profile=None):
            calls.append((mode, profile))
            return {"modes": {mode: {"status": "AVAILABLE"}}}

        items = [
            ("one", {"path": "a.wav", "params": {"mode": "voice_conversion",
                                                    "separator_profile": {"mode": "hp2"}}}),
            ("two", {"path": "b.wav", "params": {"mode": "transcribe"}}),
        ]
        defaults = {"mode": "polish"}
        with patch("core.ux_flow.route_capability_report", side_effect=report):
            blocked = preflight_queue_items(items, defaults, {}, storage=self.store)
        self.assertIsNone(blocked)
        self.assertEqual([mode for mode, _profile in calls], ["voice_conversion", "transcribe"])
        self.assertEqual(calls[0][1], {"mode": "hp2"})

    def test_missing_or_corrupt_snapshot_is_rejected_without_fallback(self):
        job, _ = self.create_history({"mode": "polish"})
        with self.assertRaises(CorruptJobError):
            clone_history_spec(job)

        spec = self.make_spec()
        missing = copy.deepcopy(spec)
        missing["effective_rvc"]["pth"] = str(self.root / "missing.pth")
        missing["spec_hash"] = digest(missing)
        job, _ = self.create_history(missing)
        with self.assertRaises((OSError, CorruptJobError)):
            clone_history_spec(job)


if __name__ == "__main__":
    unittest.main()
