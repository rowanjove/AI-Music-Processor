import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / "runtime" / "addons"))

import mido
from core.midi_cleanup_options import normalize_midi_cleanup
from core.job_spec import freeze
from core.pipeline import AudioPipeline
from core.ux_flow import mode_stages


class MIDICleanupPipelineTests(unittest.TestCase):
    def test_cleanup_stage_is_only_shown_when_frozen_option_is_enabled(self):
        self.assertNotIn("MIDICleanup", mode_stages("transcribe"))
        self.assertIn("MIDICleanup", mode_stages("transcribe", midi_cleanup=True))
        self.assertNotIn("MIDICleanup", mode_stages("voice_conversion", generate_midi=True))
        self.assertIn("MIDICleanup", mode_stages("voice_conversion", generate_midi=True, midi_cleanup=True))
        self.assertNotIn("MIDICleanup", mode_stages("voice_conversion", midi_cleanup=True))

    def test_pipeline_runs_cleanup_as_a_separate_input_hashed_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mid"
            midi = mido.MidiFile()
            midi.tracks.append(mido.MidiTrack([
                mido.Message("note_on", note=60, velocity=80, time=11),
                mido.Message("note_off", note=60, time=117),
                mido.MetaMessage("end_of_track"),
            ]))
            midi.save(source)
            original_bytes = source.read_bytes()
            reports = root / "reports"
            reports.mkdir()
            pipeline = AudioPipeline(log_callback=lambda _message: None)
            captured = {}

            def run_stage(job_id, stage, inputs, parameters, runner, progress):
                captured.update(job_id=job_id, stage=stage, inputs=inputs,
                                parameters=parameters, progress=progress)
                return runner()

            with patch.object(pipeline, "_run_stage", side_effect=run_stage):
                result = pipeline._cleanup_generated_midi(
                    "job", str(source), str(root), str(reports),
                    {"midi_cleanup": {"enabled": True, "quantize": True,
                                      "grid_division": 4, "merge_overlaps": False,
                                      "velocity_range": None}}, 0.76)

            self.assertEqual(captured["stage"], "MIDICleanup")
            self.assertEqual(captured["inputs"], {"midi": str(source)})
            self.assertEqual(captured["parameters"]["quantize"], True)
            self.assertEqual(captured["progress"], 0.76)
            self.assertNotEqual(result["midi"], str(source))
            self.assertTrue(Path(result["midi"]).is_file())
            self.assertTrue(Path(result["decision_path"]).is_file())
            self.assertEqual(source.read_bytes(), original_bytes)

    def test_explicitly_disabled_cleanup_records_skipped_and_legacy_omits_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mid"
            midi = mido.MidiFile()
            midi.tracks.append(mido.MidiTrack([mido.MetaMessage("end_of_track")]))
            midi.save(source)
            reports = root / "reports"
            reports.mkdir()
            pipeline = AudioPipeline(log_callback=lambda _message: None)
            captured = []

            def record(*args):
                captured.append(args)
                return {"skipped": True, "reason": args[4]}

            with patch.object(pipeline, "_record_skipped_stage", side_effect=record):
                disabled = pipeline._cleanup_generated_midi(
                    "job", str(source), str(root), str(reports),
                    {"midi_cleanup": {"enabled": False}}, 0.76)
                legacy = pipeline._cleanup_generated_midi(
                    "legacy", str(source), str(root), str(reports), {}, 0.76)

            self.assertEqual(disabled, {"midi": str(source)})
            self.assertEqual(legacy, {"midi": str(source)})
            self.assertEqual(len(captured), 1)
            job_id, stage, inputs, options, reason, progress = captured[0]
            self.assertEqual((job_id, stage, inputs), ("job", "MIDICleanup", {"midi": str(source)}))
            self.assertFalse(options["enabled"])
            self.assertIn("关闭", reason)
            self.assertEqual(progress, 0.76)

    def test_cleanup_settings_are_normalized_and_disabled_by_default(self):
        self.assertEqual(normalize_midi_cleanup(), {
            "enabled": False, "quantize": False, "grid_division": 4,
            "merge_overlaps": False, "velocity_range": None,
        })
        self.assertEqual(normalize_midi_cleanup({"enabled": True, "velocity_range": (40, 90)})[
            "velocity_range"], [40, 90])
        with self.assertRaises(ValueError):
            normalize_midi_cleanup({"enabled": True, "grid_division": 0})

    def test_new_transcribe_job_freezes_cleanup_policy_and_defaults_off(self):
        parameters = {
            "clean_preset": "light", "master_preset": "natural",
            "rvc_model_key": "unused", "pitch_shift": 0,
            "f0_method": "harvest", "index_rate": 0.65,
            "generate_midi": False, "output_formats": ["wav"],
            "output_sample_rate": 48000, "bpm": 120,
        }
        frozen_default = freeze(parameters, "transcribe")
        frozen_enabled = freeze(dict(parameters, midi_cleanup={
            "enabled": True, "quantize": True, "grid_division": 8,
            "merge_overlaps": True,
        }), "transcribe")
        self.assertFalse(frozen_default["midi_cleanup"]["enabled"])
        self.assertEqual(frozen_enabled["midi_cleanup"]["grid_division"], 8)
        self.assertTrue(frozen_enabled["midi_cleanup"]["merge_overlaps"])

    def test_stage_cache_identity_tracks_cleanup_implementation(self):
        from core.cache_fingerprint import fingerprints
        dependencies = fingerprints("MIDICleanup", {})
        self.assertIn(str((ROOT / "core" / "midi_cleanup.py").resolve()), dependencies)
        self.assertIn(str((ROOT / "core" / "midi_cleanup_options.py").resolve()), dependencies)


if __name__ == "__main__":
    unittest.main()
